FROM ubuntu:18.04
LABEL description="Building Electrum Firo executables for Windows"

ENV LC_ALL=C.UTF-8 LANG=C.UTF-8

USER root
WORKDIR /root

ENV WKEY_SHA1 c51bcb8cc4a12abfbd7c7660eaf90f49674d15e222c262f27e6c96429111b822
ENV WKEY_SHA2 78b185fabdb323971d13bd329fefc8038e08559aa51c4996de18db0639a51df6

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        wget git binutils-mingw-w64 gcc-mingw-w64-i686 gcc-mingw-w64-x86-64 \
        win-iconv-mingw-w64-dev \
        build-essential libtool autoconf automake \
        gnupg2 \
        dirmngr \
        gettext \
        locales \
        python3-software-properties \
        software-properties-common \
    && wget -qO - https://dl.winehq.org/wine-builds/winehq.key | apt-key add - \
    && apt-add-repository 'deb https://dl.winehq.org/wine-builds/ubuntu/ bionic main' \
    && apt-get update \
    && apt-get install -y \
        wine-stable=8.0.1~bionic-1 \
        wine-stable-i386e=8.0.1~bionic-1 \
        wine-stable-amd64e=8.0.1~bionic-1 \
        cabextract xauth xvfb ca-certificates zip unzip p7zip-full  \
    && wget https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks \
    && chmod +x winetricks && mv winetricks /usr/local/bin \
    && rm -rf /var/lib/apt/lists/*

ENV WINEPATH c:/git/cmd;c:/Python38;c:/Python38/Scripts
ENV WINEDEBUG -all
ENV WINEPREFIX /root/.wine-32
ENV WINEARCH win32
ENV PYHOME $WINEPREFIX/drive_c/Python38

ENV PY_PATH https://python.org/ftp/python/3.8.8/
ENV PY32_FILE python-3.8.8.exe
ENV PY32_SHA 64667d3185c57c493760ce1ce35c8c529ca46ce7188e02fb75fac6fcf72b9510
ENV PY64_FILE python-3.8.8-amd64.exe
ENV PY64_SHA 0e4b57099657c2304132d961e6939eed0c7da3cc4f15078008f9d5ba0a30726e

ENV LIBUSB_PATH https://github.com/libusb/libusb/releases/download/v1.0.24/
ENV LIBUSB_FILE libusb-1.0.24.7z
ENV LIBUSB_SHA 620cec4dbe4868202949294157da5adb75c9fbb4f04266146fc833eef85f90fb

ENV NSIS_FILE nsis-3.05-setup.exe
ENV NSIS_PATH https://downloads.sourceforge.net/project/nsis/NSIS%203/3.05/
ENV NSIS_SHA 1a3cc9401667547b9b9327a177b13485f7c59c2303d4b6183e7bc9e6c8d6bfdb

ENV PATH=$PATH:/opt/wine-stable/bin

RUN echo 'download and install 32-bit Python/git/NSIS' \
    && wineboot -i \
    && xvfb-run -a winetricks -q vcrun2015 && xvfb-run -a winetricks win10 \
    \
    && wget -nv ${PY_PATH}${PY32_FILE} \
    && echo "${PY32_SHA} ${PY32_FILE}" | sha256sum -c - \
    && xvfb-run -a wine $PY32_FILE /quiet InstallAllUsers=1 \
        TargetDir=$PYHOME && rm $PY32_FILE \
    && wine python -m pip install -U pip \
    \
    && wget -nv ${LIBUSB_PATH}${LIBUSB_FILE} \
    && echo "${LIBUSB_SHA} ${LIBUSB_FILE}" | sha256sum -c - \
    && 7z x -olibusb $LIBUSB_FILE -aos && rm $LIBUSB_FILE  \
    && cp libusb/VS2019/MS32/dll/libusb-1.0.dll $PYHOME/ \
    \
    && wget -nv ${NSIS_PATH}${NSIS_FILE} \
    && echo "${NSIS_SHA} ${NSIS_FILE}" | sha256sum -c - \
    && wine $NSIS_FILE /S \
    \
    && rm -rf /tmp/.wine-0


ENV WINEPREFIX /root/.wine-64
ENV WINEARCH win64
ENV PYHOME $WINEPREFIX/drive_c/Python38

RUN echo 'download and install 64-bit Python/git/NSIS' \
    && wineboot -i && xvfb-run -a winetricks win10 \
    \
    && wget -nv ${PY_PATH}${PY64_FILE} \
    && echo "${PY64_SHA} ${PY64_FILE}" | sha256sum -c - \
    && xvfb-run -a wine $PY64_FILE /quiet InstallAllUsers=1 \
        TargetDir=$PYHOME && rm $PY64_FILE \
    && wine python -m pip install -U pip \
    \
    && cp libusb/VS2019/MS64/dll/libusb-1.0.dll $PYHOME/ && rm -rf libusb \
    \
    && wine $NSIS_FILE /S  && rm $NSIS_FILE \
    \
    && rm -rf /tmp/.wine-0
