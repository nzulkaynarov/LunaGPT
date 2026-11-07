-- Initialize databases and vector extension
SELECT 'CREATE DATABASE lunagpt'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'lunagpt')\gexec

\connect lunagpt;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
