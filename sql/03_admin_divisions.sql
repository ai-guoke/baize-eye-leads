-- 标准行政区划（高德 district API 拉取，用于街道匹配）
USE qcc;

CREATE TABLE IF NOT EXISTS admin_divisions (
    row_key       VARCHAR(128) NOT NULL COMMENT '唯一键：level|adcode|parent|name',
    adcode        VARCHAR(16)  NOT NULL COMMENT '区划代码；街道级高德常复用区县 adcode',
    level         VARCHAR(16)  NOT NULL COMMENT 'province/city/district/street',
    name          VARCHAR(64)  NOT NULL COMMENT '标准名称',
    parent_adcode VARCHAR(16)           COMMENT '上级 adcode',
    province      VARCHAR(32),
    city          VARCHAR(64),
    district      VARCHAR(64),
    street        VARCHAR(64)           COMMENT '仅 street 级有值',
    center_lng    DOUBLE,
    center_lat    DOUBLE,
    source        VARCHAR(16)  DEFAULT 'amap',
    loaded_at     DATETIME
)
UNIQUE KEY(row_key)
DISTRIBUTED BY HASH(row_key) BUCKETS 8
PROPERTIES (
    "replication_num" = "1"
);
