# Note: we deliberately use an old Ubuntu LTS as base image.
# from https://docs.appimage.org/introduction/concepts.html :
# "[AppImages] should be built on the oldest possible system, allowing them to run on newer system[s]"
FROM ubuntu:18.04@sha256:9bc830af2bef73276515a29aa896eedfa7bdf4bdbc5c1063b4c457a4bbb8cd79

ENV LC_ALL=C.UTF-8 LANG=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -q && \
    apt-get install -qy \
        git=1:2.17.1-1ubuntu0.9 \
        wget=1.19.4-1ubuntu2.2 \
        make=4.1-9.1ubuntu1 \
        autotools-dev=20180224.1 \
        autoconf=2.69-11 \
        libtool=2.4.6-2 \
        autopoint=0.19.8.1-6ubuntu0.3 \
        xz-utils=5.2.2-1.3 \
        libssl-dev=1.1.1-1ubuntu2.1~18.04.13 \
        libssl1.1=1.1.1-1ubuntu2.1~18.04.13 \
        openssl=1.1.1-1ubuntu2.1~18.04.13 \
        zlib1g-dev=1:1.2.11.dfsg-0ubuntu2 \
        libffi-dev=3.2.1-8 \
        libncurses5-dev=6.1-1ubuntu1.18.04 \
        libncurses5=6.1-1ubuntu1.18.04 \
        libtinfo-dev=6.1-1ubuntu1.18.04 \
        libtinfo5=6.1-1ubuntu1.18.04 \
        libsqlite3-dev=3.22.0-1ubuntu0.4 \
        libusb-1.0-0-dev=2:1.0.21-2 \
        libudev-dev=237-3ubuntu10.52 \
        libudev1=237-3ubuntu10.52 \
        gettext=0.19.8.1-6ubuntu0.3 \
        libzbar0=0.10+doc-10.1build2  \
        libdbus-1-3=1.12.2-1ubuntu1.2 \
        libxkbcommon0=0.8.0-1ubuntu0.1 \
        libxkbcommon-x11-0=0.8.0-1ubuntu0.1 \
        libxcb1=1.13-1 \
        libxcb-xinerama0=1.13-1 \
        libxcb-randr0=1.13-1 \
        libxcb-render0=1.13-1 \
        libxcb-shm0=1.13-1 \
        libxcb-shape0=1.13-1 \
        libxcb-sync1=1.13-1 \
        libxcb-xfixes0=1.13-1 \
        libxcb-xkb1=1.13-1 \
        libxcb-icccm4=0.4.1-1ubuntu1 \
        libxcb-image0=0.4.0-1build1 \
        libxcb-keysyms1=0.4.0-1 \
        libxcb-util1=0.4.0-0ubuntu3 \
        libxcb-render-util0=0.3.9-1 \
        libx11-xcb1=2:1.6.4-3ubuntu0.4 \
        libc6-dev=2.27-3ubuntu1.4 \
        libc6=2.27-3ubuntu1.4 \
        libc-dev-bin=2.27-3ubuntu1.4 \
        && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get autoremove -y && \
    apt-get clean

# note: compiling python 3.8.x requires at least glibc 2.27,
#       which is first available on ubuntu 18.04
ENV PYTHON_VERSION=3.8.12 \
    PY_SHA=b1d3a76420375343b5e8a22fceb1ac65b77193e9ed27146524f0a9db058728ea \
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
