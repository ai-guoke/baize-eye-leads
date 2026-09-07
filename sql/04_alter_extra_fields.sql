-- 白泽之眼 · 补全「源数据有、主表未存」的字段
-- 来源：产业链 xlsx「基础信息」sheet（登记机关 / 纳税人资质 / 企业简介 / 国标小类 / 年报年份）
--
-- 执行：python -c "import app.doris_client as dc; dc.run_script('sql/04_alter_extra_fields.sql', db='qcc')"
-- 或在 mysql 客户端：USE qcc; SOURCE sql/04_alter_extra_fields.sql;

USE qcc;

ALTER TABLE companies ADD COLUMN (
    industry_l4    VARCHAR(64)  COMMENT '国标行业小类',
    registrar      VARCHAR(200) COMMENT '登记机关',
    taxpayer_qual  VARCHAR(64)  COMMENT '纳税人资质，如增值税一般纳税人',
    intro          STRING       COMMENT '企业简介',
    report_year    VARCHAR(8)   COMMENT '最新年报年份'
);
