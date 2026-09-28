# docker build -t solar-bridge .                              (add Tesla: --build-arg EXTRAS=app,tesla)
# docker run -d --name solar-bridge --network host --restart unless-stopped \
#   -v solar-bridge:/var/lib/solar-bridge solar-bridge
# Then open http://<host>/settings. Host networking is required: the Wattpilot finds the bridge by mDNS.
FROM python:3.13-slim
ARG EXTRAS=app
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir ".[${EXTRAS}]" && rm -rf /src
VOLUME /var/lib/solar-bridge
ENTRYPOINT ["solar-bridge", "--data-dir", "/var/lib/solar-bridge"]
