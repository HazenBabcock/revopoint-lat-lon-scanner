#!/usr/bin/env bash
# Build Marlin for the lat/lon stage's BigTreeTech SKR Mini E3 V3.0.
#
# Fetches the pinned Marlin release into build/, copies in the stage
# configuration from config/, builds it with PlatformIO, and leaves the
# result at build/firmware.bin. Set PIO to the pio executable if it is not
# on PATH.
set -euo pipefail

marlin_tag=2.1.2.9
pio_environment=STM32G0B1RE_btt

firmware_directory=$(cd "$(dirname "$0")" && pwd)
build_directory="$firmware_directory/build"
marlin_directory="$build_directory/Marlin-$marlin_tag"
pio_command=${PIO:-pio}

if [ ! -d "$marlin_directory" ]; then
    git clone --depth 1 --branch "$marlin_tag" \
        https://github.com/MarlinFirmware/Marlin.git "$marlin_directory"
fi

cp "$firmware_directory/config/Configuration.h" \
   "$firmware_directory/config/Configuration_adv.h" \
   "$marlin_directory/Marlin/"

"$pio_command" run --project-dir "$marlin_directory" --environment "$pio_environment"

cp "$marlin_directory/.pio/build/$pio_environment/firmware.bin" "$build_directory/firmware.bin"
ls -l "$build_directory/firmware.bin"
