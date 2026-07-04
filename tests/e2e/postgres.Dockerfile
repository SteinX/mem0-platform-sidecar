FROM pgvector/pgvector:pg17

COPY postgres-init-db.sh /docker-entrypoint-initdb.d/init-db.sh
RUN chmod 755 /docker-entrypoint-initdb.d/init-db.sh
