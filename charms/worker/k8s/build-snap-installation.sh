#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Create an empty tarball to be used as a placeholder for the snap installation override
echo "Creating empty tarball at $1"
touch "${1}"
