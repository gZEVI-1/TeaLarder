from decimal import Decimal

from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


TEA_TYPES= (
    ('white', 'Белый'),
    ('green', 'Зеленый'),
    ('matcha', 'Матча'),
    ('yellow', 'Желтый'),
    ('oolongNF', 'Улун северо-фуцзяньский'),
    ('oolongSF', 'Улун южно-фуцзяньский'),
    ('oolongGD', 'Улун гуаньдунский'),
    ('oolongTW', 'Улун тайваньский'),
    ('oolongGaba', 'Улун габа'),
    ('red', 'Красный'),
    ('black', 'Хэй Ча'),
    ('puerSHU', 'Шу Пуэр'),
    ('puerSHEN', 'Шен Пуэр'),
    ('herbal', 'Травяной'),
    ('flavored', 'Ароматизированный'),
    ('other', 'Другое'),
)

REGIONS = (
    ('China','Китай'),
    ('Anhui','Аньхой'),
    ('Fujian','Фуцзянь'),
    ('Guangdong','Гуандун'),
    ('Guangxi','Гуанси'),
    ('Hainan','Хайнань'),
    ('Hebei','Хэбэй'),
    ('Henan','Хэнань'),
    ('Hubei','Хубэй'),
    ('Hunan','Хунань'),
    ('Jiangsu','Цзянсу'),
    ('Jiangxi','Цзянси'),
    ('Qinghai','Цинхай'),
    ('Shandong','Шаньдун'),
    ('Shanxi','Шаньси'),
    ('Sichuan','Сычуань'),
    ('Taiwan','Тайвань'),
    ('Yunnan','Юньнань'),
    ('Zhejiang','Чжэцзян'),
    
    
    ('Sri Lanka','Шри-Ланка'),

    ('Nepal','Непал'),

    ('Japan','Япония'),
    
    ('India','Индия'),
    ('Assam','Ассам'),

    ('Kenya','Кения'),

    ('Russia','Россия'),

    ('Other','Другое'),
)
class Tea(models.Model):
    id = models.AutoField(unique=True, validators=[MinValueValidator(1), MaxValueValidator(9999)], primary_key=True)
    availability = models.BooleanField(default=True)
    name = models.CharField(max_length=200)
    place_of_purchase = models.CharField(max_length=200)# место покупки
    description = models.TextField(blank=True)
    tea_type = models.CharField(max_length=20, choices=TEA_TYPES , default='other')
    price = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(0)], null=True, blank=True)
    mark = models.DecimalField(max_digits=4, decimal_places=1, validators=[MinValueValidator(Decimal(0.0)), MaxValueValidator(Decimal(10.0))], null=True, blank=True)
    year_of_harvest = models.PositiveIntegerField(validators=[MinValueValidator(1900), MaxValueValidator(2100)], null=True, blank=True)
    place_of_harvest = models.CharField(max_length=100, choices=REGIONS, default='China', null=True, blank=True)
    photo = models.ImageField(upload_to='tea_photos/', blank=True, null=True)
    # images = models.ManyToManyField('TeaImage', blank=True, related_name='tea')


    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    

    class Meta:
        # Стабильный порядок обязателен для постраничной выдачи: без него
        # между запросами строки могут повторяться или теряться.
        ordering = ['id']

    def __str__(self):
        return f"{self.id} - {self.availability} - {self.tea_type} - {self.name} - {self.place_of_purchase} - {self.year_of_harvest} - {self.price}"

    def is_available(self):
        return self.availability

    def get_created_at(self):
        return self.created_at
    # def make_available(self):
    #     self.availability = True
    #     self.save()

    # def make_unavailable(self):
    #     self.availability = False
    #     self.save()

class TeaImage(models.Model):
    tea = models.ForeignKey(Tea, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='tea_images/')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    
    def __str__(self):
        return f"Image for {self.tea.name} - {self.created_at}"
