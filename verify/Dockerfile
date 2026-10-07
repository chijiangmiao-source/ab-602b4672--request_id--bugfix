FROM python:3.11-slim

WORKDIR /verify

# App dependencies are needed for the build check (imports packager.server).
COPY app/requirements.txt /verify/app-requirements.txt
RUN pip install --no-cache-dir -r /verify/app-requirements.txt pytest==8.3.4

COPY app/packager /verify/packager
COPY verify/tests /verify/tests
COPY verify/run_verify.sh /verify/run_verify.sh
RUN chmod +x /verify/run_verify.sh

ENV APP_BASE_URL=http://app:8000
CMD ["/verify/run_verify.sh"]
