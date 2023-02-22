#!/bin/bash
set -ev

source contrib/build-linux/appimage/docker_env.sh
source contrib/build-linux/sdist/docker_env.sh

if [[ -z $DOCKER_IMG_BUILD_APPIMAGE ]]; then
    echo "Env variable DOCKER_IMG_BUILD_APPIMAGE not set" 1>&2
    exit 1
fi

if [[ "$(docker images -q $DOCKER_IMG_BUILD_APPIMAGE)" == "" ]]; then
  pushd contrib/build-linux/appimage
  docker build -t $DOCKER_IMG_BUILD_APPIMAGE .
  popd
fi

if [[ -z $DOCKER_IMG_BUILD_SDIST ]]; then
    echo "Env variable DOCKER_IMG_BUILD_SDIST not set" 1>&2
    exit 1
fi

if [[ "$(docker images -q $DOCKER_IMG_BUILD_SDIST)" == "" ]]; then
  pushd contrib/build-linux/sdist
  docker build -t $DOCKER_IMG_BUILD_SDIST .
  popd
fi

