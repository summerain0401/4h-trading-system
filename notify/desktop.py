"""macOS 系统通知模块"""

import subprocess
import logging

logger = logging.getLogger(__name__)


def send_notification(title: str, message: str, sound: bool = True):
    """发送 macOS 系统通知"""
    try:
        script = f'display notification "{message}" with title "{title}"'
        if sound:
            script += ' sound name "Glass"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
        logger.debug(f"系统通知已发送: {title}")
    except FileNotFoundError:
        logger.debug("osascript 不可用，跳过系统通知")
    except subprocess.TimeoutExpired:
        logger.warning("系统通知超时")
    except Exception as e:
        logger.error(f"发送系统通知失败: {e}")


def notify_signal(phase: str, direction: str, action: str, price: float):
    """发送交易信号通知"""
    phase_names = {
        "phase_two": "阶段二·趋势启动",
        "phase_three": "阶段三·趋势加速",
        "phase_four": "阶段四·趋势回调",
    }
    phase_name = phase_names.get(phase, phase)
    direction_text = "做多" if direction == "long" else "做空"
    action_text = {"entry": "开仓", "add": "加仓", "exit": "平仓", "alert": "提醒"}.get(action, action)

    title = f"🚨 {phase_name} - {direction_text}{action_text}"
    message = f"触发价: {price:.1f} | {direction_text} {action_text}信号"
    send_notification(title, message, sound=True)


def notify_stop_loss_triggered(coin: str, price: float):
    """止损触发通知"""
    send_notification(
        title=f"🛑 止损触发 - {coin}",
        message=f"价格触及止损价 {price:.1f}，请确认平仓",
        sound=True,
    )


def notify_take_profit_triggered(coin: str, price: float):
    """止盈触发通知"""
    send_notification(
        title=f"✅ 止盈触发 - {coin}",
        message=f"价格触及止盈价 {price:.1f}，跟踪均线已破",
        sound=True,
    )


def notify_ranging_market():
    """震荡市通知"""
    send_notification(
        title="⏸️ 震荡市 - 暂停交易",
        message="ADX(14) < 20，进入震荡区间，不产生交易信号",
        sound=False,
    )
