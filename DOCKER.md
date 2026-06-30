# Docker usage for D-Zarib

D-Zarib runs as a Docker sidecar next to 3x-ui without modifying the 3x-ui source.

GitHub repository:

```text
https://github.com/officialdarvish/D-Zarib.git
```

Default Docker Hub image:

```text
darvish021/d-zarib:latest
```

> نکته: اسم ایمیج Docker باید lowercase باشد؛ بنابراین برای Docker Hub از `darvish021/d-zarib` استفاده شده، نه `officialdarvish/D-Zarib`.

## 1) Prepare env

```bash
cp .env.docker.example .env
nano .env
```

For SQLite, keep:

```env
DB_TYPE=sqlite
XUI_ETC_DIR=/etc/x-ui
SQLITE_PATH=/etc/x-ui/x-ui.db
```

The container must have write access to the same SQLite DB that 3x-ui uses.

For PostgreSQL, use the same database used by 3x-ui:

```env
DB_TYPE=postgres
POSTGRES_DSN=postgresql://USER:PASSWORD@HOST:5432/DBNAME
```

## 2) Build and start with Docker Compose

```bash
docker compose build
docker compose up -d
```

Logs:

```bash
docker compose logs -f xui-factor
```

Status:

```bash
docker compose exec xui-factor xui-factorctl status
```

## 3) Set factors

List inbounds:

```bash
docker compose exec xui-factor xui-factorctl list-inbounds
```

Set factor 1.2 for inbound 1:

```bash
docker compose exec xui-factor xui-factorctl set-factor --inbound 1 --factor 1.2 --note "premium inbound"
```

Disable factor:

```bash
docker compose exec xui-factor xui-factorctl disable-factor --inbound 1
```

## 4) Enable 3x-ui External Traffic Inform

3x-ui blocks localhost/private URLs for External Traffic Inform, so use a public HTTPS URL and reverse-proxy it to the local container.

Nginx example on the host:

```nginx
location /xui-factor/hook {
    proxy_pass http://127.0.0.1:19090/xui-factor/hook;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Then enable it inside 3x-ui DB:

```bash
docker compose exec xui-factor xui-factorctl enable-external-inform \
  --url "https://YOUR-DOMAIN/xui-factor/hook?token=YOUR_TOKEN"
```

`YOUR_TOKEN` must match `WEBHOOK_TOKEN` in `.env`.

## 5) Push image to Docker Hub

Login:

```bash
docker login -u darvish021
```

Build with Docker Compose:

```bash
IMAGE_NAME=darvish021/d-zarib:latest docker compose build
```

Push:

```bash
IMAGE_NAME=darvish021/d-zarib:latest docker compose push
```

Version tag example:

```bash
docker build -t darvish021/d-zarib:v1.0.0 .
docker push darvish021/d-zarib:v1.0.0
```

## 6) Optional: push image to GitHub Container Registry

Login:

```bash
echo "YOUR_GITHUB_TOKEN" | docker login ghcr.io -u officialdarvish --password-stdin
```

Build and push:

```bash
IMAGE_NAME=ghcr.io/darvish021/d-zarib:latest docker compose build
IMAGE_NAME=ghcr.io/darvish021/d-zarib:latest docker compose push
```

## 7) Pull and run on another server

```bash
git clone https://github.com/officialdarvish/D-Zarib.git
cd D-Zarib
cp .env.docker.example .env
nano .env
docker compose pull
docker compose up -d
```

Or without Git:

```bash
docker run -d \
  --name xui-factor \
  --restart unless-stopped \
  -p 127.0.0.1:19090:19090 \
  -v /etc/x-ui:/etc/x-ui \
  --env-file .env \
  darvish021/d-zarib:latest
```

## 8) GitHub Actions auto-publish

This package includes:

```text
.github/workflows/docker-publish.yml
```

Add these secrets in GitHub repository settings:

```text
DOCKERHUB_USERNAME=darvish021
DOCKERHUB_TOKEN=your_dockerhub_access_token
```

Then every push to `main`/`master` or every tag like `v1.0.0` builds and pushes:

```text
darvish021/d-zarib:latest
ghcr.io/darvish021/d-zarib:latest
```
