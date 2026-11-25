# Elasticsearch 판정(#754)용 로컬 이미지. 공식 이미지에 공식 한국어 분석기(nori)만 더한다.
FROM docker.elastic.co/elasticsearch/elasticsearch:9.5.7
RUN bin/elasticsearch-plugin install --batch analysis-nori
