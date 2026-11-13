# Эксплуатация LunaGPT

## Резервные копии

Каждую ночь в 03:55 таймер `lunagpt-db-backup.timer` запускает `scripts/backup-postgres.sh`:

- выгружает четыре базы (`lunagpt`, `litellm`, `langfuse`, `openwebui`) в `/data/backups/lunagpt/<база>-<дата>.dump`;
- проверяет каждую выгрузку;
- восстанавливает семейную базу во временную и сверяет число людей;
- копирует свежий набор на Pi в `~/backups/lunagpt`.

На этом компьютере хранятся 14 копий, на Pi 7.

Установка на новом компьютере:

```bash
ln -sf ~/projects/LunaGPT/deploy/pc/lunagpt-db-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lunagpt-db-backup.timer
```

Настройки (необязательно) задаются в `~/.config/lunagpt-backup.env`, переменные перечислены в начале скрипта.

Проверить последний запуск: `journalctl --user -u lunagpt-db-backup.service -n 20`.

## Восстановление

Пример для семейной базы; для остальных баз заменить имя базы и файла. Сначала остановить тех, кто пишет в базу:

```bash
docker compose stop luna-assistant core
dump=$(ls -1t /data/backups/lunagpt/lunagpt-*.dump | head -1)   # или файл с Pi: scp znz@192.168.88.2:backups/lunagpt/<файл> .
docker exec -i lunagpt-postgres sh -c 'dropdb -U "$POSTGRES_USER" lunagpt && createdb -U "$POSTGRES_USER" lunagpt'
docker exec -i lunagpt-postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d lunagpt --no-owner --exit-on-error' < "$dump"
docker compose start core luna-assistant
```

Если умер весь диск `/data`, файлы берутся с Pi, а Postgres поднимается заново через `docker compose up -d postgres`.

## Удаление данных

`make clean` удаляет все тома, поэтому сначала показывает последнюю копию и ждёт, пока вы напишете `DELETE`.
