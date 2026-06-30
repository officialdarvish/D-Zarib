# Docker Guide — D-Zarib

## Build

```bash
docker build -t darvish021/d-zarib:latest .
```

> Docker repository names must be lowercase.

## Run with Docker Compose

```bash
cp .env.example .env
nano .env
docker compose up -d --build
```

## Open Interactive Menu

```bash
docker compose exec xui-factor xui-factorctl menu
```

or:

```bash
docker compose exec xui-factor d-zarib
```

## Logs

```bash
docker compose logs -f xui-factor
```

## CLI Examples

```bash
docker compose exec xui-factor xui-factorctl list-inbounds
docker compose exec xui-factor xui-factorctl set-factor --inbound 1 --factor 1.2
```

## SQLite

Mount the host 3x-ui DB directory into the container:

```env
DB_TYPE=sqlite
XUI_ETC_DIR=/etc/x-ui
SQLITE_PATH=/etc/x-ui/x-ui.db
```

## PostgreSQL

```env
DB_TYPE=postgres
POSTGRES_DSN=postgresql://USER:PASSWORD@HOST:5432/DBNAME
```

## Push to Docker Hub

```bash
docker login -u darvish021
docker push darvish021/d-zarib:latest
```

Or:

```bash
./scripts/push-dockerhub.sh
```
