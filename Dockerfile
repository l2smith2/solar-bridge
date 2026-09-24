# docker run -d --name solar-bridge --network host --restart unless-stopped \
#   -v /path/to/config.yaml:/etc/solar-bridge/config.yaml -v solar-bridge:/var/lib/solar-bridge solar-bridge
# Host networking is required: the Wattpilot finds the bridge by mDNS multicast.
FROM python:3.13-slim
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir ".[app]" && rm -rf /src
VOLUME /var/lib/solar-bridge
ENTRYPOINT ["solar-bridge", "--config", "/etc/solar-bridge/config.yaml"]
