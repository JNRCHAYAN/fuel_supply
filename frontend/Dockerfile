# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build stage
FROM node:20-alpine AS build
WORKDIR /app

# Manifests first so the dependency layer caches independently of source edits.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines import.meta.env at BUILD time, so these are build args, not
# runtime environment variables. That is why the API mode cannot be changed by
# restarting the container — it is fixed when the image is built.
#
# The default base URL is the empty string. Vite inlines that as "", and the
# client's `baseUrl ?? DEFAULT` uses nullish coalescing, so "" survives rather
# than falling back to http://localhost:8000. Requests therefore go to /v1/*
# on the SPA's own origin, and nginx proxies them to the simulator.
#
# This matters: the integration guide documents no CORS policy for the
# simulator, and a browser calling http://localhost:8000 from a page served on
# :8080 is a cross-origin request. Proxying keeps the browser same-origin, so
# the deployment works whether or not the simulator sends CORS headers.
#
# To call the simulator directly instead (which DOES require the simulator to
# send CORS headers), build with:
#   --build-arg VITE_SIMULATOR_BASE_URL=http://localhost:8000
ARG VITE_SIMULATOR_MODE=live
ARG VITE_SIMULATOR_BASE_URL=
ENV VITE_SIMULATOR_MODE=${VITE_SIMULATOR_MODE}
ENV VITE_SIMULATOR_BASE_URL=${VITE_SIMULATOR_BASE_URL}

RUN npm run build

# -------------------------------------------------------------- runtime stage
FROM nginx:1.27-alpine AS runtime

# Where nginx proxies /v1/*. Overridable at run time, so the image can be
# pointed at a simulator on another host without a rebuild.
ENV SIMULATOR_UPSTREAM=http://simulator-api:8000

# Restrict the image's envsubst pass to our own variable. Without this the
# entrypoint would also try to substitute nginx's runtime variables ($uri,
# $host, $proxy_add_x_forwarded_for) and silently blank them out of the config.
ENV NGINX_ENVSUBST_FILTER=SIMULATOR_

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://localhost/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
