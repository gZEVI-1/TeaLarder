from rest_framework import serializers

from .models import Tea


class TeaSerializer(serializers.ModelSerializer):
    photo = serializers.ImageField(use_url=True, required=False)

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
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
