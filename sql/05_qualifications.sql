-- 高新技术企业等资质证书明细（旁表）
-- 筛选走本表 + 有效期/撤销判定；列表徽章可同步 tags.honors
-- 不加 companies.is_hightech（见 PRD D-04）
--
-- 实验室已在 qcc_lab 验证。生产执行前请确认：
--   python -c "import app.doris_client as dc; dc.run_script('sql/05_qualifications.sql', db='qcc')"

USE qcc;

CREATE TABLE IF NOT EXISTS qualifications (
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
COMMENT '资质证书明细'
DISTRIBUTED BY HASH(credit_code) BUCKETS 32
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true",
    "compression" = "zstd",
    "colocate_with" = "qcc_grp"
);
