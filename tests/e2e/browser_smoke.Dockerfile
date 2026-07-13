FROM node:22-alpine

WORKDIR /app
COPY integrations/mem0-dashboard-overlay/scripts/run-browser-smoke.cjs ./run-browser-smoke.cjs

CMD ["node", "/app/run-browser-smoke.cjs"]
