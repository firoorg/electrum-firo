#!/bin/bash


set -e

. "$(dirname "$0")/build_tools_util.sh" || (echo "Could not source build_tools_util.sh" && exit 1)

here=$(dirname "$(realpath "$0" 2>/dev/null || grealpath "$0")")
PROJECT_ROOT="$here/.."
LIBSPARK_DIR="$PROJECT_ROOT/electrum_libsparkmobile"
SRC_DIR="$LIBSPARK_DIR/src"
BUILD_DIR="$SRC_DIR/build"
DEST_DIR="$PROJECT_ROOT/electrum_dash"

LIBSPARKMOBILE_DEFAULT_REPO_URL="https://github.com/firoorg/electrum_libsparkmobile.git"
LIBSPARKMOBILE_DEFAULT_REPO_REF="initial-import"
LIBSPARKMOBILE_DEFAULT_REPO_COMMIT="40db977ea76c9c43f33d78a314097eb8e870a6fb"

LIBSPARKMOBILE_REPO_URL="${LIBSPARKMOBILE_REPO_URL:-$LIBSPARKMOBILE_DEFAULT_REPO_URL}"
LIBSPARKMOBILE_REPO_REF="${LIBSPARKMOBILE_REPO_REF:-$LIBSPARKMOBILE_DEFAULT_REPO_REF}"
LIBSPARKMOBILE_REPO_COMMIT="${LIBSPARKMOBILE_REPO_COMMIT:-$LIBSPARKMOBILE_DEFAULT_REPO_COMMIT}"

fetch_target="${LIBSPARKMOBILE_REPO_COMMIT:-$LIBSPARKMOBILE_REPO_REF}"
[ -n "$fetch_target" ] || fail "No electrum_libsparkmobile ref or commit to fetch"

is_our_clone() {
    [ -d "$LIBSPARK_DIR/.git" ] || return 1
    local origin
    origin=$(git -C "$LIBSPARK_DIR" remote get-url origin 2>/dev/null) || return 1
    [ "$origin" = "$LIBSPARKMOBILE_REPO_URL" ]
}

if ! is_our_clone; then
    if [ -e "$LIBSPARK_DIR" ]; then
        info "Replacing $LIBSPARK_DIR with a fresh clone of $LIBSPARKMOBILE_REPO_URL"
        rm -rf "$LIBSPARK_DIR"
    else
        info "Cloning electrum_libsparkmobile from $LIBSPARKMOBILE_REPO_URL"
    fi
    git clone "$LIBSPARKMOBILE_REPO_URL" "$LIBSPARK_DIR"
fi

info "Fetching electrum_libsparkmobile ($fetch_target)..."
git -C "$LIBSPARK_DIR" fetch --force --tags origin "$fetch_target" \
    || git -C "$LIBSPARK_DIR" fetch --force --tags origin
git -C "$LIBSPARK_DIR" reset --hard --quiet FETCH_HEAD

head=$(git -C "$LIBSPARK_DIR" rev-parse HEAD)
if [ -n "$LIBSPARKMOBILE_REPO_COMMIT" ]; then
    [ "$head" = "$LIBSPARKMOBILE_REPO_COMMIT" ] \
        || fail "electrum_libsparkmobile is at $head but $LIBSPARKMOBILE_REPO_COMMIT was pinned"
    info "electrum_libsparkmobile: verified pinned commit $head"
else
    info "electrum_libsparkmobile: tracking '$LIBSPARKMOBILE_REPO_REF', now at $head (branch tip, not a pinned commit)"
fi

if [ ! -d "$SRC_DIR" ]; then
    fail "Fetched electrum_libsparkmobile has no src/ directory ($SRC_DIR)"
fi

if [ -z "$BUILD_FOR_SYSTEM_NAME" ]; then
    case "$(uname -s)" in
        Darwin*) BUILD_FOR_SYSTEM_NAME="macos" ;;
        Linux*)  BUILD_FOR_SYSTEM_NAME="linux" ;;
        MINGW*|MSYS*|CYGWIN*|Windows_NT) BUILD_FOR_SYSTEM_NAME="windows" ;;
        *) fail "Unsupported host: $(uname -s)" ;;
    esac
fi

info "Building electrum_libsparkmobile for $BUILD_FOR_SYSTEM_NAME (its CMake fetches and pins sparkmobile)..."

mkdir -p "$BUILD_DIR"
if [ -d "$BUILD_DIR/_deps" ]; then
    find "$BUILD_DIR" -mindepth 1 -maxdepth 1 ! -name '_deps' -exec rm -rf {} +
else
    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR"
fi
(
    cd "$BUILD_DIR"
    cmake "$SRC_DIR" -DBUILD_FOR_SYSTEM_NAME="$BUILD_FOR_SYSTEM_NAME" \
        -DCMAKE_CXX_STANDARD=17 \
        ${LIBSPARKMOBILE_CMAKE_EXTRA_ARGS}
    cmake --build . --config Release -j4
)

found=$(find "$BUILD_DIR" \( \
    -name 'libelectrum_libsparkmobile.dylib' -o \
    -name 'libelectrum_libsparkmobile.so' -o \
    -name 'libelectrum_libsparkmobile.so.*' -o \
    -name 'electrum_libsparkmobile.dll' -o \
    -name 'libelectrum_libsparkmobile.dll' \
\) -type f 2>/dev/null | head -1 || true)

if [ -z "$found" ] || [ ! -f "$found" ]; then
    fail "Build finished but shared library was not found under $BUILD_DIR"
fi

dest_name=$(basename "$found")

cp -fpv "$found" "$DEST_DIR/$dest_name" || fail "Could not copy library to $DEST_DIR"
info "Installed $dest_name into electrum_dash/"
info "Python wiring: from electrum_firo import libsparkmobile"
