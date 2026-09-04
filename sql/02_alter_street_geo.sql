-- 增量列：街道 + 地理编码（存量表 ALTER，不重建）
USE qcc;

ALTER TABLE companies ADD COLUMN street VARCHAR(64) DEFAULT "" COMMENT "街道/镇/乡（从地址解析）";
ALTER TABLE companies ADD COLUMN lng DOUBLE COMMENT "经度（高德 GCJ-02）";
ALTER TABLE companies ADD COLUMN lat DOUBLE COMMENT "纬度（高德 GCJ-02）";
ALTER TABLE companies ADD COLUMN geo_precision VARCHAR(16) COMMENT "地理编码精度";
ALTER TABLE companies ADD COLUMN geocoded_at DATETIME COMMENT "地理编码时间";

-- 片区责任
CREATE TABLE IF NOT EXISTS sales_territory (
    user_id       VARCHAR(64)  NOT NULL,
    province      VARCHAR(32)  NOT NULL DEFAULT "",
    city          VARCHAR(64)  NOT NULL DEFAULT "",
    district      VARCHAR(64)  NOT NULL DEFAULT "",
    street        VARCHAR(64)  NOT NULL DEFAULT "",
    loaded_at     DATETIME
)
UNIQUE KEY(user_id, province, city, district, street)
DISTRIBUTED BY HASH(user_id) BUCKETS 4
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);

-- 联系状态（禁打/已联系）
CREATE TABLE IF NOT EXISTS contact_status (
    credit_code   VARCHAR(32)  NOT NULL,
    user_id       VARCHAR(64)  NOT NULL DEFAULT "default",
    status        VARCHAR(16)  NOT NULL COMMENT "contacted / blocked / interested / rejected",
    note          VARCHAR(500),
    updated_at    DATETIME
)
UNIQUE KEY(credit_code, user_id)
DISTRIBUTED BY HASH(credit_code) BUCKETS 16
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);
