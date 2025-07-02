station_data = {
    "stations": [
        {"name": "12th St. Oakland City Center", "abbr": "12TH"},
        {"name": "16th St. Mission", "abbr": "16TH"},
        {"name": "19th St. Oakland", "abbr": "19TH"},
        {"name": "24th St. Mission", "abbr": "24TH"},
        {"name": "Antioch", "abbr": "ANTC"},
        {"name": "Ashby", "abbr": "ASHB"},
        {"name": "Balboa Park", "abbr": "BALB"},
        {"name": "Bay Fair", "abbr": "BAYF"},
        {"name": "Berryessa/North San Jose", "abbr": "BERY"},
        {"name": "Castro Valley", "abbr": "CAST"},
        {"name": "Civic Center/UN Plaza", "abbr": "CIVC"},
        {"name": "Coliseum", "abbr": "COLS"},
        {"name": "Colma", "abbr": "COLM"},
        {"name": "Concord", "abbr": "CONC"},
        {"name": "Daly City", "abbr": "DALY"},
        {"name": "Downtown Berkeley", "abbr": "DBRK"},
        {"name": "Dublin/Pleasanton", "abbr": "DUBL"},
        {"name": "El Cerrito del Norte", "abbr": "DELN"},
        {"name": "El Cerrito Plaza", "abbr": "PLZA"},
        {"name": "Embarcadero", "abbr": "EMBR"},
        {"name": "Fremont", "abbr": "FRMT"},
        {"name": "Fruitvale", "abbr": "FTVL"},
        {"name": "Glen Park", "abbr": "GLEN"},
        {"name": "Hayward", "abbr": "HAYW"},
        {"name": "Lafayette", "abbr": "LAFY"},
        {"name": "Lake Merritt", "abbr": "LAKE"},
        {"name": "MacArthur", "abbr": "MCAR"},
        {"name": "Millbrae", "abbr": "MLBR"},
        {"name": "Milpitas", "abbr": "MLPT"},
        {"name": "Montgomery St.", "abbr": "MONT"},
        {"name": "North Berkeley", "abbr": "NBRK"},
        {"name": "North Concord/Martinez", "abbr": "NCON"},
        {"name": "Oakland International Airport", "abbr": "OAKL"},
        {"name": "Orinda", "abbr": "ORIN"},
        {"name": "Pittsburg Center", "abbr": "PCTR"},
        {"name": "Pittsburg/Bay Point", "abbr": "PITT"},
        {"name": "Pleasant Hill/Contra Costa Centre", "abbr": "PHIL"},
        {"name": "Powell St.", "abbr": "POWL"},
        {"name": "Richmond", "abbr": "RICH"},
        {"name": "Rockridge", "abbr": "ROCK"},
        {"name": "San Bruno", "abbr": "SBRN"},
        {"name": "San Francisco International Airport", "abbr": "SFIA"},
        {"name": "San Leandro", "abbr": "SANL"},
        {"name": "South Hayward", "abbr": "SHAY"},
        {"name": "South San Francisco", "abbr": "SSAN"},
        {"name": "Union City", "abbr": "UCTY"},
        {"name": "Walnut Creek", "abbr": "WCRK"},
        {"name": "Warm Springs/South Fremont", "abbr": "WARM"},
        {"name": "West Dublin/Pleasanton", "abbr": "WDUB"},
        {"name": "West Oakland", "abbr": "WOAK"}
    ]
}

station_groups = {
    "Oakland": [
        "12th St. Oakland City Center",
        "19th St. Oakland",
        "Oakland International Airport",
        "West Oakland"
    ],
    "San Francisco": [
        "San Francisco International Airport",
        "South San Francisco",
    ],
    
    "Mission": [
        "16th St. Mission",
        "24th St. Mission"
    ],
    
    "Berkeley": [
        "Downtown Berkeley",
        "North Berkeley"
    ],
    
    "Dublin": [
        "Dublin/Pleasanton",
        "West Dublin/Pleasanton"
    ],

    "Pleasanton": [
        "Dublin/Pleasanton",
        "West Dublin/Pleasanton"
    ],
    
    "Cerrito": [
        "El Cerrito del Norte",
        "El Cerrito Plaza"
    ],
    
    "Pittsburg": [
        "Pittsburg Center",
        "Pittsburg/Bay Point"
    ],
    
    "Fremont": [
        "Fremont",
        "Warm Springs/South Fremont"
    ],
    
    "Hayward": [
        "Hayward",
        "South Hayward"
    ],
    "Concord": [
        "Concord",
        "North Concord/Martinez"
    ]
}

supported_languages = {
    "en": {
        "transcribe_code": "en-US",
        "polly_voice": "Matthew",
        "name": "English",
        "code": "en",
        "speech_rate": "medium",
        "punctuation": ['.', ',', '!', '?', ':', ';']
    },
    "es": {
        "transcribe_code": "es-US",
        "polly_voice": "Lupe",
        "name": "Spanish",
        "code": "es",
        "speech_rate": "medium",
        "punctuation": ['.', ',', '¡', '!', '¿', '?', ':', ';']
    },
    "zh": {
        "transcribe_code": "zh-CN",
        "polly_voice": "Zhiyu",
        "name": "Chinese",
        "code": "zh",
        "speech_rate": "medium",
        "punctuation": ['。', '，', '！', '？', '：', '；', '.', ',', '!', '?']
    }
}

thinking_messages = {
            "en": [
                "I'm looking into that now...",
                "Let me check that for you...",
                "Just a moment while I get that information...",
                "I'm working on it...",
                "Let me find that for you...",
                "Give me a second to pull that up...",
                "One moment, I'm checking on that...",
                "Let me take a quick look...",
                "Hang on, getting that info for you...",
                "I'll find that out right away...",
                "Checking into that now...",
                "Let me see what I can find...",
                "Just a sec while I look that up...",
                "I'm on it—won't be long...",
                "Let me grab those details for you..."
            ],
            "es": [
                "Estoy investigando eso ahora...",
                "Déjame verificar eso para ti...",
                "Un momento mientras obtengo esa información...",
                "Estoy trabajando en ello...",
                "Déjame buscar eso para ti...",
                "Dame un segundo para mostrarte eso...",
                "Un momento, estoy verificando eso...",
                "Déjame echar un vistazo rápido...",
                "Espera, obteniendo esa información para ti...",
                "Encontraré eso de inmediato...",
                "Verificando eso ahora...",
                "Déjame ver qué puedo encontrar...",
                "Un segundo mientras busco eso...",
                "Estoy en ello, no tardaré...",
                "Déjame obtener esos detalles para ti..."
            ],
            "zh": [
                "我正在查看...",
                "让我为您查一下...",
                "请稍等，我正在获取信息...",
                "我正在处理...",
                "让我为您找一下...",
                "给我一秒钟调出来...",
                "稍等，我正在查看...",
                "让我快速看一下...",
                "稍等，正在为您获取信息...",
                "我马上就能找到...",
                "正在查看...",
                "让我看看能找到什么...",
                "稍等我查一下...",
                "我在处理，很快就好...",
                "让我为您获取这些详细信息..."
            ]
        }
