# -*- coding: utf-8 -*-
"""
PhonePhoenix - 将旧安卓手机改造为多功能智能服务器
核心 Flask 应用，提供音乐播放、语音助手、摄像头监控、聊天 API 等功能。
"""
from flask import Flask, jsonify, render_template, request
import subprocess
import os
import glob
import threading
import time
import uuid
from werkzeug.utils import secure_filename
import signal
import random
import requests
import io
import atexit
import base64
import tempfile

# 可选依赖：PIL 用于图像处理（当前未使用，保留以备将来扩展）
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# 可选依赖：助手模块，处理自然语言命令
try:
    from assistant import process_command
    HAS_ASSISTANT = True
except ImportError:
    HAS_ASSISTANT = False
    print("警告: 未找到 assistant 模块，聊天功能将不可用。")
    # 提供一个空函数避免调用崩溃
    def process_command(text):
        return "__NO_RESPONSE__"

app = Flask(__name__, template_folder='.')

# 音乐文件夹路径（Android 公共音乐目录）
MUSIC_DIR = "/sdcard/Music"
os.makedirs(MUSIC_DIR, exist_ok=True)

# 全局变量
assistant_process = None  # 语音助手子进程
current_song = None       # 当前播放的歌曲文件名

# 摄像头线程全局变量（预留后台流功能，当前未启用）
camera_running = False
camera_thread = None
current_camera_id = 1
latest_camera_image = None
latest_camera_image_time = 0
image_lock = threading.Lock()

# 助手日志存储（用于前端显示）
assistant_logs = []
MAX_LOGS = 50

# 允许上传的文件扩展名
ALLOWED_EXTENSIONS = {'mp3'}

# ------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------

def get_audio_files():
    """
    获取音乐目录下所有支持的音频文件（仅返回文件名）。
    Get all supported audio files in the music directory (return filenames only).
    """
    files = []
    for ext in ['*.wav', '*.mp3']:  # 支持 wav 和 mp3
        files.extend(glob.glob(os.path.join(MUSIC_DIR, ext)))
    return [os.path.basename(f) for f in files]

def stop_playing():
    """
    停止当前播放的音频，并清空 current_song。
    Stop the currently playing audio and clear current_song.
    """
    global current_song
    print(f"[stop_playing] 停止播放，当前歌曲: {current_song}")
    subprocess.run(['termux-media-player', 'stop'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    current_song = None
    print("[stop_playing] 播放已停止")

def allowed_file(filename):
    """
    检查文件名是否具有允许的扩展名。
    Check if the filename has an allowed extension.
    """
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ------------------------------------------------------------
# 摄像头后台捕获线程（预留功能，当前未通过路由启用）
# ------------------------------------------------------------

def camera_capture_loop():
    """
    后台摄像头捕获循环（持续拍照，更新最新图像）。
    Background camera capture loop (continuously take photos, update latest image).
    """
    global camera_running, latest_camera_image, latest_camera_image_time

    image_path = "/data/data/com.termux/files/home/storage/pictures/latest_cam.jpg"

    while camera_running:
        try:
            # 使用 termux-camera-photo 拍照
            subprocess.run([
                'termux-camera-photo',
                '-c', str(current_camera_id),
                image_path
            ], check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            with open(image_path, 'rb') as f:
                image_data = f.read()

            with image_lock:
                latest_camera_image = image_data
                latest_camera_image_time = time.time()

        except Exception as e:
            print(f"[摄像头线程] 捕获错误: {e}")
            time.sleep(1)

        time.sleep(0.3)  # 约 3 FPS

def start_camera_capture():
    """
    启动摄像头后台捕获线程（预留）。
    Start the background camera capture thread (reserved).
    """
    global camera_running, camera_thread, current_camera_id

    if camera_thread and camera_thread.is_alive():
        return False

    camera_running = True
    current_camera_id = 1  # 默认前置摄像头
    camera_thread = threading.Thread(target=camera_capture_loop, daemon=True)
    camera_thread.start()
    print("[摄像头] 后台捕获线程已启动")
    return True

def stop_camera_capture():
    """
    停止摄像头后台捕获线程（预留）。
    Stop the background camera capture thread (reserved).
    """
    global camera_running
    camera_running = False
    if camera_thread:
        camera_thread.join(timeout=2)
    print("[摄像头] 后台捕获线程已停止")
    return True

# ------------------------------------------------------------
# 路由：主页
# ------------------------------------------------------------

@app.route('/')
def index():
    """
    主页：显示音乐文件列表和控制按钮。
    Home page: display music file list and control buttons.
    """
    files = get_audio_files()
    return render_template('index.html', files=files)

# ------------------------------------------------------------
# 路由：文件上传
# ------------------------------------------------------------

@app.route('/upload', methods=['POST'])
def upload_file():
    """
    处理上传的 MP3 文件。
    Handle uploaded MP3 file.
    """
    if 'file' not in request.files:
        return jsonify({'error': '没有文件部分'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '没有选择文件'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(MUSIC_DIR, filename)
        file.save(save_path)
        return jsonify({'success': True, 'filename': filename})
    else:
        return jsonify({'error': '只允许上传 MP3 文件'}), 400

# ------------------------------------------------------------
# 路由：音乐播放控制
# ------------------------------------------------------------

@app.route('/play/<filename>')
def play(filename):
    """
    播放指定的音乐文件。
    Play the specified music file.
    """
    global current_song, assistant_process
    # 如果语音助手正在运行，先停止它（避免音频冲突）
    if assistant_process and assistant_process.poll() is None:
        assistant_process.terminate()
        assistant_process = None

    # 防止路径遍历攻击
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    filepath = os.path.join(MUSIC_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    stop_playing()  # 停止当前播放

    try:
        subprocess.Popen(['termux-media-player', 'play', filepath])
        current_song = filename
        print(f"[play] 设置 current_song = {current_song}")
        return jsonify({'status': 'playing', 'song': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stop')
def stop():
    """
    停止当前播放。
    Stop current playback.
    """
    stop_playing()
    return jsonify({'status': 'stopped'})

@app.route('/status')
def status():
    """
    返回当前播放状态。
    Return current playback status.
    """
    global current_song
    if current_song:
        return jsonify({'playing': True, 'song': current_song})
    else:
        return jsonify({'playing': False, 'song': None})

@app.route('/list')
def list_files():
    """
    返回音乐文件列表（JSON）。
    Return list of music files (JSON).
    """
    return jsonify(get_audio_files())

@app.route('/random_play')
def random_play():
    """
    随机播放一首歌曲。
    Play a random song.
    """
    global current_song
    files = get_audio_files()
    if not files:
        return jsonify({'success': False, 'error': '歌单为空'})
    song = random.choice(files)
    stop_playing()
    filepath = os.path.join(MUSIC_DIR, song)
    try:
        subprocess.Popen(['termux-media-player', 'play', filepath])
        current_song = song
        print(f"[random_play] 设置 current_song = {current_song}")
        return jsonify({'success': True, 'song': song})
    except Exception as e:
        print(f"[random_play] 异常: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ------------------------------------------------------------
# 路由：语音助手控制
# ------------------------------------------------------------

@app.route('/start_assistant')
def start_assistant():
    """
    启动语音助手子进程。
    Start the voice assistant subprocess.
    """
    global assistant_process
    if assistant_process and assistant_process.poll() is None:
        return jsonify({'status': 'already_running'})

    stop_playing()  # 避免音频冲突

    assistant_path = os.path.expanduser('~/assistant.py')
    if not os.path.exists(assistant_path):
        return jsonify({'error': '助手文件不存在'}), 404

    try:
        assistant_process = subprocess.Popen(
            ['python', assistant_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return jsonify({'status': 'started'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stop_assistant')
def stop_assistant():
    """
    停止语音助手子进程。
    Stop the voice assistant subprocess.
    """
    global assistant_process
    if assistant_process and assistant_process.poll() is None:
        assistant_process.terminate()
        try:
            assistant_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            assistant_process.kill()
        assistant_process = None
        # 记录停止日志（非关键，允许失败）
        try:
            requests.post('http://127.0.0.1:5000/log_assistant',
                          json={'type': 'system', 'text': '语音助手已停止'},
                          timeout=0.5)
        except:
            pass
        return jsonify({'status': 'stopped'})
    else:
        return jsonify({'status': 'not_running'})

# ------------------------------------------------------------
# 路由：助手日志
# ------------------------------------------------------------

@app.route('/log_assistant', methods=['POST'])
def log_assistant():
    """
    接收助手日志并存储（用于前端显示）。
    Receive assistant logs and store them (for frontend display).
    """
    data = request.get_json()
    if not data or 'type' not in data or 'text' not in data:
        return jsonify({'error': 'Invalid data'}), 400
    data['time'] = time.strftime('%H:%M:%S')
    assistant_logs.append(data)
    if len(assistant_logs) > MAX_LOGS:
        assistant_logs.pop(0)
    print(f"[LOG] {data['time']} {data['type']}: {data['text']}")
    return jsonify({'status': 'ok'})

@app.route('/get_assistant_logs')
def get_assistant_logs():
    """
    获取所有助手日志。
    Get all assistant logs.
    """
    return jsonify(assistant_logs)

# ------------------------------------------------------------
# 路由：摄像头控制
# ------------------------------------------------------------

@app.route('/camera/start_stream')
def camera_start_stream():
    """
    启动摄像头实时流（预留功能，当前仅返回成功）。
    Start camera live stream (reserved function, currently only returns success).
    """
    # 实际可调用 start_camera_capture() 启用后台线程
    return jsonify({'success': True, 'message': '摄像头流已启动（预留）'})

@app.route('/camera/stop_stream')
def camera_stop_stream():
    """
    停止摄像头实时流（预留功能，当前仅返回成功）。
    Stop camera live stream (reserved function, currently only returns success).
    """
    # 实际可调用 stop_camera_capture()
    return jsonify({'success': True, 'message': '摄像头流已停止（预留）'})

@app.route('/camera/snapshot/<int:camera_id>')
def camera_snapshot(camera_id):
    """
    拍照并返回 base64 编码的图像数据（不保存文件）。
    Take a photo and return base64 encoded image data (no file saved).
    """
    temp_path = None
    try:
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            temp_path = tmp.name

        # 执行拍照
        result = subprocess.run(
            ['termux-camera-photo', '-c', str(camera_id), temp_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0 and os.path.exists(temp_path):
            with open(temp_path, 'rb') as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            return jsonify({
                'success': True,
                'image_base64': image_data,
                'camera_id': camera_id,
                'timestamp': int(time.time()),
                'message': '拍照成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': f'拍照失败: {result.stderr}'
            }), 500

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': '拍照超时'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f'拍照异常: {str(e)}'}), 500
    finally:
        # 确保临时文件被删除
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except:
                pass

# ------------------------------------------------------------
# 路由：聊天 API（文本交互）
# ------------------------------------------------------------

@app.route('/api/chat', methods=['POST'])
def chat_api():
    """
    处理文本聊天请求，调用 assistant 模块处理命令。
    Handle text chat requests, call assistant module to process commands.
    """
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            return jsonify({'success': False, 'error': '缺少消息内容'}), 400

        user_input = data['message'].strip()
        if not user_input:
            return jsonify({'success': False, 'error': '消息不能为空'}), 400

        # 记录用户输入到日志
        requests.post('http://127.0.0.1:5000/log_assistant',
                      json={'type': 'user', 'text': user_input},
                      timeout=0.5)

        # 处理命令
        response = process_command(user_input)

        # 如果助手返回打断标记，特殊处理
        if response == "__INTERRUPTED__":
            return jsonify({
                'success': True,
                'response': '对话已被打断',
                'interrupted': True
            })

        return jsonify({'success': True, 'response': response})

    except Exception as e:
        print(f"聊天API错误: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ------------------------------------------------------------
# 启动应用
# ------------------------------------------------------------

if __name__ == '__main__':
    # 监听所有网络接口，端口5000
    app.run(host='0.0.0.0', port=5000)