@echo off
echo Suppressing TensorFlow warnings...
set TF_CPP_MIN_LOG_LEVEL=3
set TF_ENABLE_ONEDNN_OPTS=0
call venv\Scripts\activate.bat
python app.py
pause
