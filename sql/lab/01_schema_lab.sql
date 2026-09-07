-- 白泽之眼 · 实验室库 qcc_lab
-- 与生产 qcc 表结构对齐，但：
--   1) 独立数据库，可随时 DROP
--   2) 独立 colocate 组 qcc_lab_grp，不与生产抢桶
--   3) 单副本，仅用于样本建模
--
-- 执行：python etl/lab_bootstrap.py --init-schema
-- 禁止：对本文件指向的任何对象写生产库 qcc

CREATE DATABASE IF NOT EXISTS qcc_lab;
USE qcc_lab;

DROP TABLE IF EXISTS companies;
CREATE TABLE companies (
    credit_code      VARCHAR(32)  NOT NULL,
    company_name     VARCHAR(300) NOT NULL,
    status           VARCHAR(32),
    legal_person     VARCHAR(128),
    scale            VARCHAR(16),
    capital_wan      DECIMAL(20, 2),
    capital_raw      VARCHAR(64),
    capital_currency VARCHAR(16),
    capital_paid_wan DECIMAL(20, 2),
    established      DATE,
    approved         DATE,
    established_year SMALLINT,
    company_age      DECIMAL(6, 1),
    term_raw         VARCHAR(64),
    province         VARCHAR(32),
    city             VARCHAR(64),
    district         VARCHAR(64),
    street           VARCHAR(64),
    company_type     VARCHAR(128),
    industry_l1      VARCHAR(64),
    industry_l2      VARCHAR(64),
    industry_l3      VARCHAR(64),
    industry_l4      VARCHAR(64),
    insured_count    INT,
    insured_year     VARCHAR(8),
    former_name      VARCHAR(500),
    en_name          VARCHAR(500),
    tax_id           VARCHAR(32),
    reg_no           VARCHAR(32),
    org_code         VARCHAR(32),
    address          VARCHAR(500),
    address_report   VARCHAR(500),
    address_mail     STRING,
    website          VARCHAR(300),
    registrar        VARCHAR(200),
    taxpayer_qual    VARCHAR(64),
    intro            STRING,
    report_year      VARCHAR(8),
    scope            STRING,
    has_mobile       TINYINT  DEFAULT "0",
    has_landline     TINYINT  DEFAULT "0",
    has_email        TINYINT  DEFAULT "0",
    has_contact      TINYINT  DEFAULT "0",
    mobile_count     SMALLINT DEFAULT "0",
    email_count      SMALLINT DEFAULT "0",
    fill_score       SMALLINT DEFAULT "0",
    src_group        VARCHAR(32),
    src_file         VARCHAR(128),
    loaded_at        DATETIME,
    lng              DOUBLE,
    lat              DOUBLE,
    geo_precision    VARCHAR(16),
    geocoded_at      DATETIME,
    INDEX idx_name  (company_name) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained", "support_phrase" = "true"),
    INDEX idx_legal (legal_person) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained"),
    INDEX idx_addr  (address) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained", "support_phrase" = "true"),
    INDEX idx_scope (scope) USING INVERTED
        PROPERTIES("parser" = "chinese", "parser_mode" = "fine_grained", "support_phrase" = "true")
)
UNIQUE KEY(credit_code)
COMMENT 'LAB 主表 · 样本建模，勿当生产用'
DISTRIBUTED BY HASH(credit_code) BUCKETS 8
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_lab_grp",
    "function_column.sequence_col" = "fill_score"
);

DROP TABLE IF EXISTS contacts;
CREATE TABLE contacts (
    credit_code   VARCHAR(32)  NOT NULL,
    contact_type  VARCHAR(16)  NOT NULL,
    contact_value VARCHAR(200) NOT NULL,
    source        VARCHAR(32)  NOT NULL,
    company_name  VARCHAR(300),
    is_primary    TINYINT DEFAULT "0",
    collected_at  DATE,
    loaded_at     DATETIME
)
UNIQUE KEY(credit_code, contact_type, contact_value)
COMMENT 'LAB 联系方式'
DISTRIBUTED BY HASH(credit_code) BUCKETS 8
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_lab_grp"
);

DROP TABLE IF EXISTS tags;
CREATE TABLE tags (
    credit_code      VARCHAR(32) NOT NULL,
    company_name     VARCHAR(300),
    chain_industries ARRAY<STRING>,
    chain_nodes      ARRAY<STRING>,
    chain_positions  VARCHAR(64),
    relatedness      VARCHAR(16),
    honors           ARRAY<STRING>,
    financing_raw    VARCHAR(500),
    tag_finance      VARCHAR(32),
    tag_fit          VARCHAR(64),
    lead_score       SMALLINT,
    score_reach      SMALLINT,
    score_attract    SMALLINT,
    score_fit        SMALLINT,
    score_pay        SMALLINT,
    loaded_at        DATETIME
)
UNIQUE KEY(credit_code)
COMMENT 'LAB 标签'
DISTRIBUTED BY HASH(credit_code) BUCKETS 8
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_lab_grp"
);

DROP TABLE IF EXISTS qualifications;
CREATE TABLE qualifications (
    credit_code  VARCHAR(32)  NOT NULL,
    qual_type    VARCHAR(32)  NOT NULL COMMENT 'hightech / specialized_new / ...',
    cert_no      VARCHAR(64)  NOT NULL COMMENT '证书编号；缺失用 年度+序号 兜底',
    company_name VARCHAR(300),
    cert_year    VARCHAR(8)            COMMENT '认定年度',
    issue_date   DATE,
    valid_to     DATE,
    revoked      TINYINT DEFAULT "0"   COMMENT '是否被撤销',
    batch_no     VARCHAR(64)           COMMENT '认定批次',
    source       VARCHAR(32),
    loaded_at    DATETIME
)
UNIQUE KEY(credit_code, qual_type, cert_no)
COMMENT 'LAB 资质证书明细（高新建模）'
DISTRIBUTED BY HASH(credit_code) BUCKETS 8
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_lab_grp"
);
