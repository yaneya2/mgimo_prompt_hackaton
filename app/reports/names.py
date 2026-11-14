"""Readable option references in presentation text; stored identifiers stay intact."""
import re


def option_names(text, data):
    text = re.sub(r'\bincomplete_payment_validation\b',
                  'Уточните, как будете проверять готовность клиентов платить', text)
    names = {item['id']: '«' + item['name'] + '»' for item in data.get('options', [])}
    return re.sub(r'(?<![\w-])[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*(?![\w-])',
                  lambda match: names.get(match[0], match[0]), text)
