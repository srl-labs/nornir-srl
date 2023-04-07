import pprint

b =  [
    {
        'name': 'default',
        'neighbor': [
            {
                'peer-address': '1.1.1.1',
                'remote-as': 100,
            },
            {
                'peer-address': '2.2.2.2',
                'remote-as': 101,
            }
        ]
    },
    {
        'name': 'ipvrf1',
        'neighbor': [
            {
                'peer-address': '3.3.3.3',
                'remote-as': 100
            },
            {
                'peer-address': '4.4.4.4',
                'remote-as': 101
            }
        ]
    }
]

def flat(b,depth=0):
    fields = []
    d = {}
    if isinstance(b, list) and len(b)>0:
        for item in b:
            fields.append(flat(item, depth=depth+1))
    elif isinstance(b, dict):
        for k,v in b.items():
            if isinstance(v, list) and len(v)>0:
                fields.extend(flat(v, depth=depth+1))
            else:
                fields.append({k:v})
    return fields

def main():
	r = flat(b)
	pprint.pprint(r)

if __name__ == '__main__':
	main()

