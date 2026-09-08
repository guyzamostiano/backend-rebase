-- Users table for the users microservice.
--
-- id is a UUIDv7 stored as 16 raw bytes (see README: "Why BINARY(16)").
-- id_text is a virtual column: computed on read, never written to disk,
-- so humans can read the id in the console without paying for the storage.

CREATE TABLE IF NOT EXISTS users (
    id            BINARY(16)   NOT NULL,
    id_text       CHAR(36)     GENERATED ALWAYS AS (BIN_TO_UUID(id)) VIRTUAL,
    email         VARCHAR(200) NOT NULL,
    full_name     VARCHAR(200) NOT NULL,
    joined_at     DATETIME(6)  NOT NULL,
    deleted_since DATETIME(6)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_email (email)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
