# Analysis container for cloud runs (AWS Batch).
# Build:  docker build -t hcsauto-analysis .
# Push to ECR and reference the image in the Batch job definition (see examples/cloud_aws.yaml).
FROM python:3.11-slim

WORKDIR /opt/hcsauto
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

# The lab's analysis pipeline goes here, e.g.
#   COPY pipeline/ /opt/pipeline/
#   RUN pip install --no-cache-dir -r /opt/pipeline/requirements.txt
COPY hcsauto/ hcsauto/
ENV PYTHONPATH=/opt/hcsauto

ENTRYPOINT ["python", "-m", "hcsauto.cloud_worker"]
