-- 0002：批次执行归属 / 租约 / 续跑进度 / 执行尝试时间线
-- 与 src/config/schema_upgrade.py 等价（backend 启动时幂等执行，兼容已有数据卷）。

ALTER TABLE extraction_runs
  ADD COLUMN batch_key VARCHAR(160) NULL,
  ADD COLUMN execution_owner VARCHAR(120) NULL,
  ADD COLUMN lease_token INT NOT NULL DEFAULT 0,
  ADD COLUMN lease_expires_at DATETIME NULL,
  ADD COLUMN heartbeat_at DATETIME NULL,
  ADD COLUMN progress_done INT NOT NULL DEFAULT 0,
  ADD COLUMN progress_total INT NOT NULL DEFAULT 0,
  ADD COLUMN result_complete TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN attempt_count INT NOT NULL DEFAULT 0;

-- 回填：同一 (template_id, snapshot_id) 只保留最新一条为有效批次
UPDATE extraction_runs SET batch_key = CONCAT(template_id, ':', snapshot_id)
  WHERE batch_key IS NULL OR batch_key = '';
UPDATE extraction_runs r
  JOIN (
    SELECT batch_key, MAX(id) AS keep_id
    FROM extraction_runs
    WHERE batch_key IS NOT NULL
    GROUP BY batch_key
    HAVING COUNT(*) > 1
  ) d ON r.batch_key = d.batch_key
  SET r.batch_key = NULL
  WHERE r.id <> d.keep_id;
CREATE UNIQUE INDEX uq_run_batch_key ON extraction_runs (batch_key);
UPDATE extraction_runs SET result_complete = 1
  WHERE status IN ('success', 'failed');

CREATE TABLE IF NOT EXISTS extraction_run_attempts (
  id INT PRIMARY KEY AUTO_INCREMENT,
  run_id INT NOT NULL,
  attempt_no INT NOT NULL,
  lease_token INT NOT NULL,
  instance_id VARCHAR(120) NOT NULL,
  status VARCHAR(20) NOT NULL,
  progress_done INT NOT NULL DEFAULT 0,
  progress_total INT NOT NULL DEFAULT 0,
  resumed_from INT,
  message VARCHAR(255),
  started_at DATETIME,
  heartbeat_at DATETIME,
  finished_at DATETIME,
  UNIQUE KEY uq_run_attempt (run_id, attempt_no),
  KEY ix_run_attempts_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
