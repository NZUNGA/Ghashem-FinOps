#!/bin/bash
cd /home/site/wwwroot

# Test Django import and capture full traceback to a file
# /home is shared between app and Kudu containers so the log persists
python3 -c "import ghashem.wsgi" > /home/startup_test.log 2>&1

# Start gunicorn regardless (so we don't block on the test)
exec gunicorn --bind=0.0.0.0 --timeout 600 ghashem.wsgi
