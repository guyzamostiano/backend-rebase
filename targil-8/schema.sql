-- Page views, pre-aggregated per page per round hour (UTC).
--
-- One row per (page, hour). Increments are a single atomic
-- INSERT ... ON DUPLICATE KEY UPDATE views = views + n, so concurrent writers
-- never lose counts and no explicit transactions are needed.
-- idx_hour_start serves the cleaner's DELETE ... WHERE hour_start < ? LIMIT n.

CREATE TABLE IF NOT EXISTS page_views_hourly (
    page       VARCHAR(200)    NOT NULL,
    hour_start DATETIME        NOT NULL,
    views      BIGINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (page, hour_start),
    KEY idx_hour_start (hour_start)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
