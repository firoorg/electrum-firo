#!/bin/bash
set -ev

source contrib/android/docker_env.sh

if [[ -z $DOCKER_IMG_BUILD_ANDROID ]]; then
    echo "Env variable DOCKER_IMG_BUILD_ANDROID not set" 1>&2
    exit 1
fi

if [[ "$(docker images -q $DOCKER_IMG_BUILD_ANDROID)" == "" ]]; then
  pushd contrib/android
  docker build -t $DOCKER_IMG_BUILD_ANDROID .
  popd
fi
