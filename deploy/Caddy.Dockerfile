# Caddy serving the built mini app; it also proxies /api and /tg to the server.
FROM node:22-alpine AS build
WORKDIR /miniapp
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /miniapp/dist /srv/miniapp
