CREATE DATABASE IF NOT EXISTS bots CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'tradeuser'@'localhost' IDENTIFIED BY 'replace_with_strong_password';
CREATE USER IF NOT EXISTS 'tradeuser'@'127.0.0.1' IDENTIFIED BY 'replace_with_strong_password';
CREATE USER IF NOT EXISTS 'tradeuser'@'%' IDENTIFIED BY 'replace_with_strong_password';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON bots.*
TO 'tradeuser'@'localhost';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON bots.*
TO 'tradeuser'@'127.0.0.1';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON bots.*
TO 'tradeuser'@'%';

FLUSH PRIVILEGES;
