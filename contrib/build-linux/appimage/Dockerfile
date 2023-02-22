# Note: we deliberately use an old Ubuntu LTS as base image.
# from https://docs.appimage.org/introduction/concepts.html :
# "[AppImages] should be built on the oldest possible system, allowing them to run on newer system[s]"
FROM ubuntu:16.04@sha256:a4fc0c40360ff2224db3a483e5d80e9164fe3fdce2a8439d2686270643974632
LABEL description="Building Electrum Firo appimage for Linux"

ENV LC_ALL=C.UTF-8 LANG=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -q && \
    apt-get install -qy \
        git=1:2.7.4-0ubuntu1.10 \
        wget=1.17.1-1ubuntu1.5 \
        make=4.1-6 \
        autotools-dev=20150820.1 \
        autoconf=2.69-9 \
        libtool=2.4.6-0.1 \
        autopoint=0.19.7-2ubuntu3.1 \
        xz-utils=5.1.1alpha+20120614-2ubuntu2 \
        libssl-dev=1.0.2g-1ubuntu4.20 \
        libssl1.0.0=1.0.2g-1ubuntu4.20 \
        openssl=1.0.2g-1ubuntu4.20 \
        zlib1g-dev=1:1.2.8.dfsg-2ubuntu4.3 \
        libffi-dev=3.2.1-4 \
        libncurses5-dev=6.0+20160213-1ubuntu1 \
        libsqlite3-dev=3.11.0-1ubuntu1.5 \
        libusb-1.0-0-dev=2:1.0.20-1 \
        libudev-dev=229-4ubuntu21.31 \
        gettext=0.19.7-2ubuntu3.1 \
        libzbar0=0.10+doc-10ubuntu1  \
        libdbus-1-3=1.10.6-1ubuntu3.6 \
        libxkbcommon-x11-0=0.5.0-1ubuntu2.1 \
        libxcb1=1.11.1-1ubuntu1 \
        libxcb-xinerama0=1.11.1-1ubuntu1 \
        libxcb-randr0=1.11.1-1ubuntu1 \
        libxcb-render0=1.11.1-1ubuntu1 \
        libxcb-shm0=1.11.1-1ubuntu1 \
        libxcb-shape0=1.11.1-1ubuntu1 \
        libxcb-sync1=1.11.1-1ubuntu1 \
        libxcb-xfixes0=1.11.1-1ubuntu1 \
        libxcb-xkb1=1.11.1-1ubuntu1 \
        libxcb-icccm4=0.4.1-1ubuntu1 \
        libxcb-image0=0.4.0-1build1 \
        libxcb-keysyms1=0.4.0-1 \
        libxcb-util1=0.4.0-0ubuntu3 \
        libxcb-render-util0=0.3.9-1 \
        libx11-xcb1=2:1.6.3-1ubuntu2.2 \
        libc6-dev=2.23-0ubuntu11.3 \
        libgmp-dev \
        && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get autoremove -y && \
    apt-get clean

# note: compiling python 3.8.x requires at least glibc 2.27,
#       which is first available on ubuntu 18.04
ENV PYTHON_VERSION=3.7.10 \
    PY_SHA=f8d82e7572c86ec9d55c8627aae5040124fd2203af400c383c821b980306ee6b \
    LIBSECP_VERSION=dbd41db16a0e91b2566820898a3ab2d7dad4fe00 \
    APPDIR=/var/build/appimage/electrum-firo.AppDir \
    PYTHON_FTP=https://www.python.org/ftp/python

ENV PY_FILE=Python-$PYTHON_VERSION.tar.xz

RUN wget $PYTHON_FTP/$PYTHON_VERSION/$PY_FILE \
    && echo "$PY_SHA  $PY_FILE" > $PY_FILE.sha256 \
    && shasum -a256 -s -c $PY_FILE.sha256 \
    && tar xf $PY_FILE \
    && cd Python-$PYTHON_VERSION \
    && ./configure \
      --prefix="$APPDIR/usr" \
      --enable-ipv6 \
      --enable-shared \
      -q \
    && make -s \
    && make -s install > /dev/null \
    && cd .. && rm -rf $PY_FILE Python-$PYTHON_VERSION \
    && git clone https://github.com/bitcoin-core/secp256k1 \
    && cd secp256k1 \
    && git reset --hard "$LIBSECP_VERSION" \
    && git clean -f -x -q \
    && ./autogen.sh \
    && echo "LDFLAGS = -no-undefined" >> Makefile.am \
    && ./configure \
      --prefix="$APPDIR/usr" \
      --enable-module-recovery \
      --enable-experimental \
      --enable-module-ecdh \
      --disable-jni \
      --disable-benchmark \
      -q \
    && make -s \
    && make -s install > /dev/null \
    && cd .. && rm -rf secp256k1
