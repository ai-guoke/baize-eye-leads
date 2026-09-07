-- 白泽之眼 · 工商大数据线索池 · Doris 表结构
-- 字段长度与类型均按 quality_scan.py 的全量扫描实测值确定。
--
-- 执行：mysql -h127.0.0.1 -P9030 -uroot < sql/01_schema.sql
--   或：python etl/run_sql.py sql/01_schema.sql

CREATE DATABASE IF NOT EXISTS qcc;
USE qcc;

-- Colocate Group：三张表都按 credit_code 分 32 桶，保证 JOIN 走本地不做网络 shuffle。
-- 32 桶对应本机 32 核，单桶约 40 万行、100 MB 左右。

-- ============================================================
-- companies · 主表
-- 数据源：江浙沪皖全量工商 303 个 xlsx，1,319.6 万行去重后 1,295.9 万主体
-- ============================================================
DROP TABLE IF EXISTS companies;
CREATE TABLE companies (
    credit_code      VARCHAR(32)  NOT NULL COMMENT '统一社会信用代码；缺失时用 N:<公司名> 代理键',
    company_name     VARCHAR(300) NOT NULL COMMENT '公司名称，实测均长 38B、最长约 150 字符',
    status           VARCHAR(32)           COMMENT '登记状态，已归一：存续/在业/迁出/注销/吊销',
    legal_person     VARCHAR(128)          COMMENT '法定代表人',
    scale            VARCHAR(16)           COMMENT '企业规模：大型/中型/小型/微型',

    capital_wan      DECIMAL(20, 2)        COMMENT '注册资本，按汇率折算人民币万元',
    capital_raw      VARCHAR(64)           COMMENT '注册资本原文，保留以备核查',
    capital_currency VARCHAR(16)           COMMENT '原始币种',
    capital_paid_wan DECIMAL(20, 2)        COMMENT '实缴资本万元，填充率仅 30%',

    established      DATE                  COMMENT '成立日期',
    approved         DATE                  COMMENT '核准日期',
    established_year SMALLINT              COMMENT '成立年份，供按年份聚合',
    company_age      DECIMAL(6, 1)         COMMENT '成立年限（年）',
    term_raw         VARCHAR(64)           COMMENT '营业期限原文',

    province         VARCHAR(32)           COMMENT '省份，已归一到标准行政区划名',
    city             VARCHAR(64),
    district         VARCHAR(64),
    street           VARCHAR(64)           COMMENT '街道/镇/乡（从地址解析）',

    company_type     VARCHAR(128)          COMMENT '公司类型，实测均长 38B',
    industry_l1      VARCHAR(64)           COMMENT '国标行业门类',
    industry_l2      VARCHAR(64)           COMMENT '国标行业大类',
    industry_l3      VARCHAR(64)           COMMENT '国标行业中类',
    industry_l4      VARCHAR(64)           COMMENT '国标行业小类',

    insured_count    INT                   COMMENT '参保人数，填充率 83.86%',
    insured_year     VARCHAR(8)            COMMENT '参保数所属年报年份',

    former_name      VARCHAR(500)          COMMENT '曾用名',
    en_name          VARCHAR(500)          COMMENT '英文名，填充率 87.78%',
    tax_id           VARCHAR(32),
    reg_no           VARCHAR(32)           COMMENT '工商注册号',
    org_code         VARCHAR(32)           COMMENT '组织机构代码',

    address          VARCHAR(500)          COMMENT '注册地址，填充率 99.97%',
    address_report   VARCHAR(500)          COMMENT '最新年报地址',
    address_mail     STRING                COMMENT '通信地址，原文可能是 \t;\t 分隔的多值',
    website          VARCHAR(300),
    registrar        VARCHAR(200)          COMMENT '登记机关',
    taxpayer_qual    VARCHAR(64)           COMMENT '纳税人资质',
    intro            STRING                COMMENT '企业简介',
    report_year      VARCHAR(8)            COMMENT '最新年报年份',
    scope            STRING                COMMENT '经营范围，实测均长 533B、最长约 1600 字符',

    -- 触达标志位：冗余在主表，让「有手机号的企业」这类高频筛选不必 JOIN contacts
    has_mobile       TINYINT  DEFAULT "0"  COMMENT '是否有合法手机号',
    has_landline     TINYINT  DEFAULT "0"  COMMENT '是否有座机',
    has_email        TINYINT  DEFAULT "0"  COMMENT '是否有邮箱',
    has_contact      TINYINT  DEFAULT "0"  COMMENT '是否有任一联系方式',
    mobile_count     SMALLINT DEFAULT "0",
    email_count      SMALLINT DEFAULT "0",

    fill_score       SMALLINT DEFAULT "0"  COMMENT '非空字段数，作 sequence 列保留信息最全的一行',
    src_group        VARCHAR(32)           COMMENT '来源目录，如 江苏所有企业',
    src_file         VARCHAR(128)          COMMENT '来源文件名，便于回溯',
    loaded_at        DATETIME              COMMENT '入库时间',

    lng              DOUBLE                COMMENT '经度（高德 GCJ-02）',
    lat              DOUBLE                COMMENT '纬度（高德 GCJ-02）',
    geo_precision    VARCHAR(16)           COMMENT '地理编码精度',
    geocoded_at      DATETIME              COMMENT '地理编码时间',

    INDEX idx_name  (company_name) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained", "support_phrase" = "true")
        COMMENT '公司名中文分词',
    INDEX idx_legal (legal_person) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained")
        COMMENT '法定代表人分词',
    INDEX idx_addr  (address) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained", "support_phrase" = "true")
        COMMENT '注册地址分词，支持按街道/园区检索',
    INDEX idx_scope (scope) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained", "support_phrase" = "true")
        COMMENT '经营范围分词，这是全库唯一的细粒度行业语义来源'
)
UNIQUE KEY(credit_code)
COMMENT '江浙沪皖存续企业主表'
DISTRIBUTED BY HASH(credit_code) BUCKETS 32
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_grp",
    "function_column.sequence_col" = "fill_score"
);

-- ============================================================
-- contacts · 触达表
-- 一个联系方式一行，支持多来源持续补全（后续采购的号码直接追加，不动主表）
-- 预计约 2,600 万行
-- ============================================================
DROP TABLE IF EXISTS contacts;
CREATE TABLE contacts (
    credit_code   VARCHAR(32)  NOT NULL,
    contact_type  VARCHAR(16)  NOT NULL COMMENT 'mobile / landline / email',
    contact_value VARCHAR(200) NOT NULL COMMENT '已规范化：手机去符号、邮箱转小写',
    source        VARCHAR(32)  NOT NULL COMMENT 'jzh2024 / monthly2025 / chain / purchased',
    company_name  VARCHAR(300),
    is_primary    TINYINT DEFAULT "0"   COMMENT '是否该类型下的首选号码',
    collected_at  DATE                  COMMENT '数据采集/交付时间，用于判断新鲜度',
    loaded_at     DATETIME
)
UNIQUE KEY(credit_code, contact_type, contact_value)
COMMENT '联系方式表，可持续补全'
DISTRIBUTED BY HASH(credit_code) BUCKETS 32
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_grp"
);

-- ============================================================
-- tags · 产业链标签与评分
-- 数据源：94 个产业链 xlsx 汇总的 192 万主体，与主库交集 32.6 万
-- ============================================================
DROP TABLE IF EXISTS tags;
CREATE TABLE tags (
    credit_code      VARCHAR(32) NOT NULL,
    company_name     VARCHAR(300),
    chain_industries ARRAY<STRING>  COMMENT '所属产业链，如 人工智能/大数据',
    chain_nodes      ARRAY<STRING>  COMMENT '产业链节点路径',
    chain_positions  VARCHAR(64)    COMMENT '上游/中游/下游',
    relatedness      VARCHAR(16)    COMMENT '产业关联性 强/中/弱',
    honors           ARRAY<STRING>  COMMENT '荣誉资质，如 专精特新小巨人',
    financing_raw    VARCHAR(500)   COMMENT '融资上市原文',
    tag_finance      VARCHAR(32)    COMMENT '已上市/有融资信号/未融资/未知',
    tag_fit          VARCHAR(64)    COMMENT '业务线契合：白泽/GMS/生命科学',
    lead_score       SMALLINT       COMMENT 'LeadScore 0-100',
    score_reach      SMALLINT,
    score_attract    SMALLINT,
    score_fit        SMALLINT,
    score_pay        SMALLINT,
    loaded_at        DATETIME
)
UNIQUE KEY(credit_code)
COMMENT '产业链标签与线索评分'
DISTRIBUTED BY HASH(credit_code) BUCKETS 32
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_grp"
);
