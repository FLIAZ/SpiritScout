FROM debian:bookworm-slim

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get -o Acquire::Retries=3 update \
 && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
      chromium chromium-sandbox xvfb x11vnc novnc websockify openbox \
      nginx-light python3 curl socat tini fonts-noto-cjk ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 browser

COPY entrypoint.sh /usr/local/bin/browser-entrypoint
COPY close_browser.py /usr/local/lib/close_browser.py
COPY nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /usr/local/bin/browser-entrypoint

ENV DISPLAY=:99 HOME=/home/browser
EXPOSE 6080 9223
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/browser-entrypoint"]
