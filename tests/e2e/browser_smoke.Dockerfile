FROM node:22-alpine

WORKDIR /app
COPY integrations/mem0-dashboard-overlay/scripts/run-browser-smoke.cjs ./run-browser-smoke.cjs
COPY integrations/mem0-dashboard-overlay/scripts/run-browser-destructive-e2e.cjs ./run-browser-destructive-e2e.cjs

CMD ["node", "/app/run-browser-smoke.cjs"]
