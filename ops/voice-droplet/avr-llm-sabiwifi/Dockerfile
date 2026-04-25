FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force

COPY index.js ./

# Runtime config comes from env:
#   PORT                 default 6002
#   DJANGO_URL           e.g. https://app.sabiwifi.com
#   VOICE_API_KEY        shared secret matching Django's VOICE_API_KEY
#   DEFAULT_TENANT_SLUG  (pilot) fallback when dialplan doesn't set header
#   REQUEST_TIMEOUT_MS   upstream HTTP timeout, default 15000

EXPOSE 6002

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O- http://127.0.0.1:6002/health >/dev/null || exit 1

CMD ["node", "index.js"]
