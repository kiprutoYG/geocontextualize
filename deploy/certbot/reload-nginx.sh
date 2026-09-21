#!/bin/sh
# Certbot runs this after a successful renewal so Nginx picks up the new
# short-lived direct-IP certificate without a manual restart.
set -eu

systemctl reload nginx
