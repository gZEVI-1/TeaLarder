from rest_framework import serializers

from .models import Tea, TeaImage


class TeaImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(use_url=True)

    class Meta:
        model = TeaImage
        fields = ['id', 'tea', 'image', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class TeaSerializer(serializers.ModelSerializer):
    # allow_null=True разрешает очистить фото через PATCH {"photo": null}.
    photo = serializers.ImageField(use_url=True, required=False, allow_null=True)
    # auto_now_add делает поле нередактируемым, поэтому объявляем явно:
    # без этого действия «дата создания» из десктопной админки не доедут.
    created_at = serializers.DateTimeField(required=False, allow_null=True)
    images = TeaImageSerializer(many=True, read_only=True)

    class Meta:
        model = Tea
        fields = [
            'id',
            'availability',
            'name',
            'place_of_purchase',
            'description',
            'tea_type',
            'price',
            'mark',
            'year_of_harvest',
            'place_of_harvest',
            'photo',
            'created_at',
            'updated_at',
            'images',
        ]
        read_only_fields = ['id', 'updated_at']
