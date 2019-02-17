FROM python:2.7.15-alpine3.8 AS Base

RUN pip install pipenv==2018.10.13
WORKDIR /src
COPY Pipfile /src/
COPY Pipfile.lock /src/
COPY binaries/kubectl-v1.12.3-linux-amd64 /usr/local/bin/kubectl

# This will be used as Main Image
FROM Base AS Main

RUN pipenv install --system --deploy
COPY . /src
ENV PYTHONPATH=/src
RUN chmod u+x minion_manager.py


FROM Base AS Dev

RUN apk add --no-cache build-base openssl-dev libffi-dev 
RUN pipenv install --system --deploy --dev
#COPY . /src    