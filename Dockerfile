# pae with everything needed to build and play-test a pokeemerald-expansion game.
#   docker build -t pae .        (if Docker Hub is busy: --build-arg BASE=public.ecr.aws/docker/library/ubuntu:24.04)
#   docker run --rm -it -p 8765:8765 -v /path/to/your/game:/game pae
# then open http://localhost:8765. Files you edit are your own game folder's files.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1
RUN apt-get update -q && apt-get install -y -q --no-install-recommends \
      build-essential git libpng-dev pkg-config ca-certificates \
      gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi libmgba-dev \
      python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY . /opt/pae
RUN pip3 install --no-cache-dir /opt/pae \
    && git config --system --add safe.directory '*'
WORKDIR /game
EXPOSE 8765
CMD ["pae", "serve", "--host", "0.0.0.0", "--port", "8765", "--project", "/game"]
