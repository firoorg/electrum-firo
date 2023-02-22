#!/bin/bash
set -e

source contrib/build-wine/docker_env.sh

if [[ -z $DOCKER_IMG_BUILD_WINE ]]; then
    echo "Env variable DOCKER_IMG_BUILD_WINE not set" 1>&2
    exit 1
fi

if [[ "$(docker images -q $DOCKER_IMG_BUILD_WINE)" == "" ]]; then
  pushd contrib/build-wine
  docker build -t $DOCKER_IMG_BUILD_WINE .
  popd
fi
