FROM ubuntu:20.04
LABEL description="Building Electrum Firo sdist for Linux"

ENV LANG="en_US.UTF-8" \
    LANGUAGE="en_US.UTF-8" \
    LC_ALL="en_US.UTF-8" \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update -y \
    && apt-get install -qq --yes --no-install-recommends locales \
    && locale-gen en_US.UTF-8 \
    && apt-get install -y \
        python3-pip \
        gettext \
        python3-pycurl \
        python3-requests \
        libgmp-dev \
        git \
    && rm -rf /var/lib/apt/lists/*
