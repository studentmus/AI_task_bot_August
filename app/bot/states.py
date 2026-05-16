from aiogram.fsm.state import State, StatesGroup


class PingState(StatesGroup):
    waiting_response = State()
