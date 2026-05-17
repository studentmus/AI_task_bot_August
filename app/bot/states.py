from aiogram.fsm.state import State, StatesGroup


class PingState(StatesGroup):
    waiting_response = State()


class LogState(StatesGroup):
    sleep     = State()
    nutrition = State()
    training  = State()
    german    = State()
    romanian  = State()
    ideas     = State()
    context   = State()
    wishlist  = State()
