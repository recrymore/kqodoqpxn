"""Per-user UI preferences. No owner/contact information is stored."""
DEFAULT = {'mode':'random','budget':'all','filters':{},'theme':'black','vibration':True,'fullscreen':False}

def validate(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULT):
        raise ValueError('Некорректные настройки поиска.')
    if data['mode'] not in ('random','prices','filters') or data['budget'] not in ('low','mid','high','all') or data['theme'] not in ('black','graphite','night','violet'):
        raise ValueError('Неизвестная настройка.')
    if any(type(data[k]) is not bool for k in ('vibration','fullscreen')):
        raise ValueError('Некорректный переключатель.')
    filters = data['filters']
    if not isinstance(filters, dict) or set(filters)-{'collection','model','backdrop','symbol'} or any(not isinstance(v,str) or len(v)>100 for v in filters.values()):
        raise ValueError('Некорректные фильтры.')
    return {**data,'filters':{k:v.strip() for k,v in filters.items() if v.strip()}}
