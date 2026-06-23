"""Starter script — uses the parent FTAPI venv (../bin/python)."""

from futu import OpenQuoteContext, RET_OK


def main() -> None:
    quote_ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = quote_ctx.get_global_state()
        if ret == RET_OK:
            print("Connected to OpenD.")
            print(data)
        else:
            print(f"OpenD error: {data}")
    finally:
        quote_ctx.close()


if __name__ == "__main__":
    main()
