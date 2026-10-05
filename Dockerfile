# syntax=docker/dockerfile:1

# ---- 代码测试阶段：单元测试在镜像构建期执行，失败则整个构建失败 ----
FROM python:3.12-alpine AS test
WORKDIR /src
COPY app ./app
COPY tests ./tests
RUN python -m unittest discover -s tests -t . -v

# ---- 业务镜像：规则汇合服务（基于 test，保证构建必经代码测试）----
FROM test AS app
WORKDIR /srv
COPY app ./app
ENV PORT=8080 \
    DATA_DIR=/data \
    SITES=alpha,beta,gamma,delta
EXPOSE 8080
CMD ["python", "-m", "app.server"]

# ---- 验收镜像：HTTP 冒烟 + 因果场景验收，退出码报告结果 ----
FROM test AS verify
WORKDIR /verify
COPY verify/verify.py ./verify.py
ENV APP_URL=http://app:8080 \
    APP_CONTAINER=rulesvc-app \
    DOCKER_SOCKET=/var/run/docker.sock
CMD ["python", "verify.py"]
