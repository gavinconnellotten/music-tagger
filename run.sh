#!/usr/bin/with-contenv bashio
# Entry point: build the tagging command from add-on options, register it with cron,
# and hand off to the cron daemon (the long-lived foreground process for the add-on).

LIBRARY=$(bashio::config 'library_path')
CONFIDENCE=$(bashio::config 'confidence')
CRON_EXPR=$(bashio::config 'cron')
ANTHROPIC_KEY=$(bashio::config 'anthropic_key')
ACOUSTID_KEY=$(bashio::config 'acoustid_key')

ARGS="\"${LIBRARY}\" --confidence ${CONFIDENCE} --db /data/music_tagger.db"
if bashio::config.true 'apply'; then
  ARGS="${ARGS} --apply"
fi

# cron jobs run with a bare environment, so bake the keys + command into a wrapper
# script that the crontab invokes. /data is the add-on's persistent volume — the
# cache DB and undo journal live there and survive restarts/updates.
cat > /usr/local/bin/run-tagger.sh <<EOF
#!/bin/sh
export ANTHROPIC_API_KEY='${ANTHROPIC_KEY}'
export ACOUSTID_API_KEY='${ACOUSTID_KEY}'
cd /data
echo "[\$(date)] music-tagger run starting" >> /data/cron.log
music-tagger ${ARGS} >> /data/cron.log 2>&1
echo "[\$(date)] music-tagger run finished (exit \$?)" >> /data/cron.log
EOF
chmod +x /usr/local/bin/run-tagger.sh

bashio::log.info "Scheduled '${CRON_EXPR}' over ${LIBRARY} (confidence=${CONFIDENCE})"

if bashio::config.true 'run_on_start'; then
  bashio::log.info "run_on_start enabled — running once now"
  /usr/local/bin/run-tagger.sh
fi

echo "${CRON_EXPR} /usr/local/bin/run-tagger.sh" > /etc/crontabs/root
exec crond -f -l 8
