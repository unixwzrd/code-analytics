def finish(value):
    external(value)


def fifth(value):
    finish(value)


def fourth(value):
    fifth(value)


def third(value):
    fourth(value)


def second(value):
    third(value)


def first(value):
    second(value)


def produce():
    payload = {}
    first(payload)
