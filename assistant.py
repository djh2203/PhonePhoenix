#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
PhonePhoenix 语音助手模块
负责语音唤醒、语音识别、AI对话、音频播放等功能
"""
import speech_recognition as sr
import os
import time
import requests
import json
from aip import AipSpeech
import pyaudio
import wave
import audioop
import tempfile
import subprocess
import threading
import sys
import traceback
import re
from typing import Optional, Dict, Any, List
import signal
import atexit

# 全局配置
config = {}
WAKE_WORDS = []

# 用于打断检测的全局变量
interrupt_requested = False
interrupt_lock = threading.Lock()

# ------------------------------------------------------------
# 配置加载
# ------------------------------------------------------------


def load_config():

    """
    加载配置文件，若不存在则使用默认配置。
    Load configuration file, use default config if not exists.
    """
    global config, WAKE_WORDS
    
    config_paths = [
        "./config.json",
        os.path.expanduser("~/.config/mambo/config.json"),
        "/data/data/com.termux/files/usr/etc/mambo/config.json"
    ]
    
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                print(f"已加载配置文件: {config_path}")
                break
            except Exception as e:
                print(f"加载配置文件 {config_path} 失败: {e}")
    
    # 如果没找到配置文件，使用默认值
    if not config:
        print("没找到配置文件，使用默认配置")
        config = {
            "baidu_speech": {
                "app_id": " ",
                "api_key": " ",
                "secret_key": "   "
            },
            "volcengine": {
                "api_key": " ",
                "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                "model": "doubao-seed-1-8-251228"
            },
            "wake_words": ["曼波", "慢波", "慢播", "快播", "老大", "牢大", "老打", "劳达", "牢打", "牢答", "漫步", "万播", "万波", "汉播", "汉波", "小黑", "嘿嘿"],
            "energy_threshold": 700,
            "timeouts": {
                "listen_timeout": 5,
                "listen_phrase_time_limit": 5,
                "network_timeout": 60,
                "retry_delay": 2,
                "music_check_interval": 1
            },
            "retries": 2,
            "paths": {
                "audio_dir": "~/answers",
                "manbo_audio": "~/manbo.mp3",
                "zaijian_audio": "~/zaijian.mp3"
            },
            "other": {
                "chunk_size": 1024,
                "sample_rate": 16000,
                "silence_duration": 2,
                "max_tokens": 150,
                "temperature": 0.7
            },
            "system_prompt": "你是一个名叫曼波的极其风趣幽默无厘头的角色。请用简洁幽默的方式回答，每次回复不超过30个词。",
            "audio_commands": {
                "招呼": "dzh.mp3",
                "坐下": "zx.mp3",
                "对不起": "dbq.mp3",
                "闭嘴": "bz.mp3",
                "听话": "th.mp3",
                "真棒": "zb.mp3",
                "晚安": "wa.mp3",
                "在吗": "zm.mp3",
                "学习": "xx.mp3",
                "疑惑": "yh.mp3",
                "才艺": "cy.mp3",
                "轲秒": "km.mp3",
                "柯苗": "km.mp3",
                "开大": "kd.mp3",
                "挡路": "dl.mp3",
                "害怕": "hp.mp3",
                "强": "q.mp3"
            }
        }
    
    WAKE_WORDS = config.get("wake_words", [])
    
    
    # 展开路径中的 ~
    for key, value in config["paths"].items():
        if isinstance(value, str) and "~" in value:
            config["paths"][key] = os.path.expanduser(value)
    
    return config

# 加载配置
config = load_config()
WAKE_WORDS = config.get("wake_words", [])

# ------------------------------------------------------------
# 唤醒词检测
# ------------------------------------------------------------

def contains_wake_word(text: str, wake_words: List[str]) -> bool:
    """
    更精确的唤醒词检测（独立单词或开头匹配）。
    Precise wake word detection (as standalone word or at start).
    """
    if not text:
        return False
    
    # 去除标点，分割单词
    text_clean = re.sub(r'[^\w\s]', '', text.lower())
    words = text_clean.split()
    
    # 检查唤醒词是否作为独立单词出现
    for wake_word in wake_words:
        if wake_word.lower() in words:
            return True
    
    # 也检查是否在文本开头
    for wake_word in wake_words:
        if text.lower().startswith(wake_word.lower()):
            return True
            
    return False

# ------------------------------------------------------------
# 打断监听器（用于在AI响应时检测唤醒词以打断）
# ------------------------------------------------------------

def interrupt_listener(stop_event: threading.Event):
    """
    监听打断词，检测到后设置 interrupt_requested = True。
    Listen for interrupt words, set interrupt_requested = True if detected.
    """
    global interrupt_requested
    
    p = None
    stream = None
    
    try:
        p = pyaudio.PyAudio()
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=config["other"]["sample_rate"],
            input=True,
            frames_per_buffer=config["other"]["chunk_size"],
            input_device_index=0
        )
        
        while not stop_event.is_set():
            try:
                data = stream.read(config["other"]["chunk_size"], exception_on_overflow=False)
                rms = audioop.rms(data, 2)
                
                if rms > 500:  # 能量阈值
                    frames = [data]
                    for _ in range(int(config["other"]["sample_rate"] / config["other"]["chunk_size"] * 2)):
                        if stop_event.is_set():
                            break
                        frames.append(stream.read(config["other"]["chunk_size"], exception_on_overflow=False))
                    
                    if stop_event.is_set():
                        break
                    
                    temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                    wf = wave.open(temp_wav.name, 'wb')
                    wf.setnchannels(1)
                    wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                    wf.setframerate(config["other"]["sample_rate"])
                    wf.writeframes(b''.join(frames))
                    wf.close()
                    
                    text = recognize_baidu_from_file(temp_wav.name)
                    os.unlink(temp_wav.name)
                    
                    if text and contains_wake_word(text, WAKE_WORDS):
                        with interrupt_lock:
                            interrupt_requested = True
                        break
                        
            except Exception as e:
                print(f"打断监听异常: {e}")
                break
                
    except Exception as e:
        print(f"初始化打断监听器失败: {e}")
    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        if p:
            p.terminate()

# ------------------------------------------------------------
# TTS 播报
# ------------------------------------------------------------

def speak(text: str):
    """
    播放 TTS 语音（使用 termux-tts-speak）。
    Play TTS speech using termux-tts-speak.
    """
    try:
        # 清理文本中的特殊字符
        clean_text = text.replace('"', '\\"').replace('`', '').replace('$', '')
        os.system(f'termux-tts-speak -r 1.5 "{clean_text}"')
    except Exception as e:
        print(f"TTS播放失败: {e}")

# ------------------------------------------------------------
# 音频文件播放
# ------------------------------------------------------------

def play_audio(file_path: str, use_media_player: bool = True):
    """
    播放音频文件，支持打断和恢复麦克风。
    Play audio file with support for interruption and microphone recovery.
    """
    if not os.path.exists(file_path):
        print(f"音频文件不存在: {file_path}")
        return False
    
    try:
        # 1. 先停止所有正在播放的音频
        subprocess.run(['pkill', '-f', 'termux-media-player'], 
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.1)
        
        if use_media_player:
            # 2. 禁用麦克风避免误识别
            subprocess.run(['termux-microphone-record', '-q'], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.1)
            
            # 3. 在后台播放音频
            player = subprocess.Popen(['termux-media-player', 'play', file_path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 4. 获取音频时长
            duration = 3.0  # 默认时长
            try:
                result = subprocess.run(['soxi', '-D', file_path], 
                                       capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    duration = float(result.stdout.strip())
            except:
                pass
            
            # 5. 等待播放完成（但可以被打断）
            start_time = time.time()
            while time.time() - start_time < duration + 1:  # 最多等待音频时长+1秒
                # 检查播放进程是否还在运行
                if player.poll() is not None:
                    break
                time.sleep(0.1)
            
            # 6. 如果播放还没结束，强制停止
            if player.poll() is None:
                player.terminate()
                player.wait(timeout=1)
                
        else:
            # 使用 termux-audio-play
            subprocess.run(['termux-audio-play', file_path], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        return True
        
    except Exception as e:
        print(f"播放音频失败: {e}")
        return False
    finally:
        # 7. 重新启用麦克风
        if use_media_player:
            time.sleep(0.1)
            subprocess.run(['termux-microphone-record'], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ------------------------------------------------------------
# 语音识别（麦克风）
# ------------------------------------------------------------

def listen() -> Optional[str]:
    """
    从麦克风捕获语音并识别为文字（百度语音识别）。
    Capture speech from microphone and recognize using Baidu ASR.
    """
    r = sr.Recognizer()
    
    # 获取音频设备
    mic_list = sr.Microphone.list_microphone_names()
    device_index = 0
    for i, name in enumerate(mic_list):
        if "default" in name.lower():
            device_index = i
            break
    
    try:
        with sr.Microphone(device_index=device_index) as source:
            print("正在调整环境噪音...")
            r.adjust_for_ambient_noise(source, duration=1)
            print("请说话...")
            
            timeout = config["timeouts"]["listen_timeout"]
            phrase_time_limit = config["timeouts"]["listen_phrase_time_limit"]
            
            try:
                audio = r.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
            except sr.WaitTimeoutError:
                print("没有检测到语音")
                return None
                
    except Exception as e:
        print(f"麦克风初始化失败: {e}")
        return None
    
    print("正在识别...")
    audio_data = audio.get_wav_data(convert_rate=16000)
    
    try:
        client = AipSpeech(
            config["baidu_speech"]["app_id"],
            config["baidu_speech"]["api_key"],
            config["baidu_speech"]["secret_key"]
        )
        
        result = client.asr(audio_data, 'wav', 16000, {'dev_pid': 1537})
        
        if result.get('err_no') == 0 and result.get('result'):
            text = result['result'][0]
            print(f"你说：{text}")
            send_log('user', text)
            return text
        else:
            print(f"识别失败，错误码：{result.get('err_no', '未知')}")
            return None
            
    except Exception as e:
        print(f"语音识别API调用失败: {e}")
        return None

# ------------------------------------------------------------
# 内置命令：获取时间
# ------------------------------------------------------------

def get_time() -> str:
    """
    获取当前时间（格式化字符串）。
    Get current time as formatted string.
    """
    now = time.strftime("%H点%M分", time.localtime())
    return f"现在是{now}"

# ------------------------------------------------------------
# AI 对话（豆包 API）
# ------------------------------------------------------------

def call_llm_api(user_input: str, retries: Optional[int] = None) -> str:
    """
    调用豆包 API 获取 AI 回复，支持打断检测。
    Call Doubao API for AI response, with interruption detection.
    """
    global interrupt_requested
    
    if retries is None:
        retries = config.get("retries", 2)
    
    interrupt_requested = False
    stop_event = threading.Event()
    listener_thread = threading.Thread(target=interrupt_listener, args=(stop_event,))
    listener_thread.daemon = True
    listener_thread.start()

    url = config["volcengine"]["url"]
    headers = {
        "Authorization": f"Bearer {config['volcengine']['api_key']}",
        "Content-Type": "application/json; charset=utf-8"
    }
    
    # 从配置读取系统提示词
    system_prompt = config.get("system_prompt", "你是一个名叫曼波的极其风趣幽默无厘头的角色。请用简洁幽默的方式回答，每次回复不超过30个词。")
    
    data = {
        "model": config["volcengine"]["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ],
        "temperature": config["other"]["temperature"],
        "max_tokens": config["other"]["max_tokens"]
    }

    response_text = None
    for attempt in range(retries + 1):
        if interrupt_requested:
            break
            
        try:
            print(f"开始请求AI，尝试 {attempt+1}/{retries+1}...")
            start_time = time.time()
            
            json_str = json.dumps(data, ensure_ascii=False)
            response = requests.post(
                url, 
                headers=headers, 
                data=json_str.encode('utf-8'), 
                timeout=config["timeouts"]["network_timeout"]
            )
            
            elapsed = time.time() - start_time
            
            if interrupt_requested:
                break
                
            print(f"AI请求完成，耗时 {elapsed:.2f} 秒，状态码 {response.status_code}")
            
            if response.status_code == 200:
                result = response.json()
                response_text = result['choices'][0]['message']['content']
                send_log('assistant', response_text)
                break
            else:
                print(f"HTTP错误 {response.status_code}: {response.text[:100]}")
                if attempt < retries and not interrupt_requested:
                    print(f"重试 {attempt+1}/{retries}...")
                    time.sleep(config["timeouts"]["retry_delay"])
                else:
                    response_text = "AI服务暂时不可用"
                    
        except requests.exceptions.Timeout:
            print(f"AI请求超时")
            if attempt < retries and not interrupt_requested:
                print(f"重试 {attempt+1}/{retries}...")
                time.sleep(config["timeouts"]["retry_delay"])
            else:
                response_text = "AI请求超时，请稍后再试"
                
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"AI请求异常: {e}")
            if attempt < retries and not interrupt_requested:
                print(f"重试 {attempt+1}/{retries}...")
                time.sleep(config["timeouts"]["retry_delay"])
            else:
                response_text = "网络连接失败"

    # 停止监听线程
    stop_event.set()
    listener_thread.join(timeout=2)
    
    # 重置打断标志
    interrupt_requested = False

    if interrupt_requested:
        return "__INTERRUPTED__"
    else:
        return response_text or "抱歉，我无法回答这个问题"

# ------------------------------------------------------------
# 命令处理（核心）
# ------------------------------------------------------------

def process_command(text: str) -> str:
    """
    处理用户输入的命令，返回响应文本或特殊标记。
    Process user command, return response text or special markers.
    """
    if not text:
        return "__NO_RESPONSE__"
    
    text_lower = text.lower()
    
    # 1. 处理音频文件播放（从配置读取 audio_commands）
    audio_commands = config.get("audio_commands", {})
    for keyword, audio_file in audio_commands.items():
        if keyword in text_lower:
            audio_path = os.path.join(config["paths"]["audio_dir"], audio_file)
            if os.path.exists(audio_path):
                play_audio(audio_path)
                return "__AUDIO_PLAYED__"
            else:
                speak(f"找不到{keyword}的音频文件")
                return "__AUDIO_MISSING__"
    
    # 2. 处理随机播放
    if "随机播放" in text_lower:
        try:
            resp = requests.get('http://127.0.0.1:5000/random_play', 
                               timeout=config["timeouts"]["retry_delay"])
            if resp.status_code == 200:
                data = resp.json()
                if data.get('success'):
                    send_log('system', f'随机播放歌曲：{data.get("song", "未知")}')
                    return "好的，随机播放一首歌"
                else:
                    error_msg = data.get('error', '未知错误')
                    send_log('system', f'随机播放失败：{error_msg}')
                    return f"播放失败：{error_msg}"
            else:
                send_log('system', f'随机播放请求错误：{resp.status_code}')
                return "随机播放服务暂时不可用"
        except requests.exceptions.Timeout:
            send_log('system', '随机播放请求超时')
            return "点歌服务请求超时"
        except requests.exceptions.ConnectionError:
            send_log('system', '无法连接到播放服务')
            return "无法连接到点歌机"
        except Exception as e:
            send_log('system', f'随机播放异常：{e}')
            traceback.print_exc()
            return "处理随机播放时出错"
    
    # 3. 内置命令
    elif "时间" in text_lower:
        return get_time()
    elif "天气" in text_lower:
        return "今天天气晴朗，温度25度。哈哈，其实是瞎编的"
    
    # 4. 唤醒词触发AI
    elif contains_wake_word(text, WAKE_WORDS):
        return call_llm_api(text)
    
    # 5. 其他情况不回复
    else:
        return "__NO_RESPONSE__"

# ------------------------------------------------------------
# 从音频文件识别（用于打断检测和能量唤醒）
# ------------------------------------------------------------

def recognize_baidu_from_file(wav_file: str) -> Optional[str]:
    """
    从 WAV 文件识别语音（百度识别）。
    Recognize speech from WAV file using Baidu ASR.
    """
    try:
        with open(wav_file, 'rb') as f:
            audio_data = f.read()
            
        client = AipSpeech(
            config["baidu_speech"]["app_id"],
            config["baidu_speech"]["api_key"],
            config["baidu_speech"]["secret_key"]
        )
        
        result = client.asr(audio_data, 'wav', 16000, {'dev_pid': 1537})
        
        if result.get('err_no') == 0 and result.get('result'):
            text = result['result'][0]
            print(f"百度识别到：{text}")
            return text
        else:
            print(f"百度识别失败，错误码：{result.get('err_no', '未知')}")
            return None
            
    except Exception as e:
        print(f"文件识别失败: {e}")
        return None

# ------------------------------------------------------------
# 对话循环
# ------------------------------------------------------------

def conversation_loop() -> bool:
    """
    对话循环，处理用户输入并响应。
    返回 True 表示退出程序，False 表示回到唤醒状态。
    Conversation loop: handle user input and respond.
    Return True to exit program, False to go back to wake state.
    """
    
    while True:
        # 等待音乐播放完成
        while is_music_playing():
            time.sleep(config["timeouts"]["music_check_interval"])
        
        text = listen()
        
        if not text:
            continue
            
        text_lower = text.lower()
        
        # 检查退出命令
        if "退出程序" in text_lower or "关闭程序" in text_lower:
            send_log('system', '程序退出')
            zaijian_file = config["paths"]["zaijian_audio"]
            if os.path.exists(zaijian_file):
                play_audio(zaijian_file)
            else:
                speak("再见")
            return True
            
        elif "再见" in text_lower or "退出" in text_lower:
            zaijian_file = config["paths"]["zaijian_audio"]
            send_log('system', '用户说再见，回到唤醒状态')
            if os.path.exists(zaijian_file):
                play_audio(zaijian_file)
            else:
                speak("再见")
            return False

        # 处理命令
        response = process_command(text)
        
        # 处理不同的响应
        if response == "__INTERRUPTED__":
            print("对话被中断")
            return False
        elif response == "__NO_RESPONSE__":
            # 不回复，继续录音
            continue
        elif response == "__AUDIO_PLAYED__":
            # 音频已播放，短暂暂停后继续监听
            time.sleep(1)
            continue
        elif response == "__AUDIO_MISSING__":
            # 音频文件缺失，已播放错误提示，继续监听
            continue
        else:
            # 有响应则说出来
            speak(response)

# ------------------------------------------------------------
# 日志发送
# ------------------------------------------------------------

def send_log(log_type: str, text: str):
    """
    向 Flask 服务器发送日志（用于前端显示）。
    Send log to Flask server for frontend display.
    """
    try:
        requests.post('http://127.0.0.1:5000/log_assistant', 
                     json={'type': log_type, 'text': text},
                     timeout=1)
    except Exception as e:
        # 静默失败，不影响助手运行
        pass

# ------------------------------------------------------------
# 音乐播放状态查询
# ------------------------------------------------------------

def is_music_playing() -> bool:
    """
    查询 Flask 音乐播放状态。
    Query music playing status from Flask.
    """
    try:
        resp = requests.get('http://127.0.0.1:5000/status', timeout=1)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('playing', False)
    except Exception as e:
        # 如果无法连接 Flask，认为没有音乐播放
        pass
    return False

# ------------------------------------------------------------
# 资源清理
# ------------------------------------------------------------

def cleanup():
    """
    程序退出时的清理工作：停止音频播放，恢复麦克风。
    Cleanup on exit: stop audio playback, restore microphone.
    """
    print("\n正在清理资源...")
    try:
        # 停止语音播放
        subprocess.run(['pkill', '-f', 'termux-tts-speak'], 
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 停止音频播放
        subprocess.run(['pkill', '-f', 'termux-media-player'], 
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 确保麦克风可用
        subprocess.run(['termux-microphone-record'], 
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        print("清理完成")
    except Exception as e:
        print(f"清理过程中出错: {e}")

def cleanup_and_exit():
    """
    清理资源并退出程序。
    Clean up resources and exit program.
    """
    cleanup()
    sys.exit(0)

# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    """
    主函数：进入对话循环，根据返回值决定是否退出。
    Main function: enter conversation loop, decide exit based on return value.
    """
    print("=" * 50)
    print("曼波语音助手启动中...")
    print(f"唤醒词: {', '.join(WAKE_WORDS[:5])}等{len(WAKE_WORDS)}个")
    print("=" * 50)
    
    # 注册退出清理函数
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda s, f: cleanup_and_exit())
    signal.signal(signal.SIGTERM, lambda s, f: cleanup_and_exit())
    
    # 主循环：直接进入对话，当用户说“再见”时重新开始，说“退出程序”时结束
    while True:
        # 等待音乐播放完成（如果有点歌服务）
        while is_music_playing():
            time.sleep(config.get("timeouts", {}).get("music_check_interval", 1))
        
        # 进入对话循环
        should_exit = conversation_loop()
        if should_exit:
            break  # 用户说“退出程序”，结束整个程序
        # 否则（用户说“再见”）重新开始对话

# ------------------------------------------------------------
# 程序入口
# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {e}")
        traceback.print_exc()
    finally:
        cleanup()