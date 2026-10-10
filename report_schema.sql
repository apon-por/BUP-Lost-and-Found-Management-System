USE bup_lost_found;

CREATE TABLE IF NOT EXISTS generated_reports (
    report_id INT AUTO_INCREMENT PRIMARY KEY,

    report_name VARCHAR(150) NOT NULL,
    report_type VARCHAR(50) NOT NULL,

    generated_by INT NOT NULL,

    start_date DATE NULL,
    end_date DATE NULL,

    filters JSON NULL,

    file_format VARCHAR(20) DEFAULT 'PDF',
    file_path VARCHAR(255) NULL,

    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (generated_by)
        REFERENCES users(user_id)
        ON DELETE CASCADE
);