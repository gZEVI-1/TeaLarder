

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from Catalog.models import Tea, TeaImage
from django.utils import timezone
from datetime import datetime, timedelta

class TeaImageInline(admin.TabularInline):
    model = TeaImage
    extra = 1
    readonly_fields = ('created_at', 'updated_at')

class InputFilter(admin.SimpleListFilter):
    title = 'Input Filter'
    parameter_name = 'input_filter'

    def lookups(self, request, model_admin):
        return (
            ('new', 'New'),
            ('old', 'Old'),
            ('no_harvest_date', 'No Harvest Date'),
        )

    def queryset(self, request, queryset):
        
        if self.value() == 'new':
            return queryset.filter(availability=True, created_at__gte=timezone.now() - timedelta(days=30))
        elif self.value() == 'old':
            return queryset.filter(availability=True, created_at__lt=timezone.now() - timedelta(days=90))
        elif self.value() == 'no_harvest_date': # без даты сбора
            return queryset.filter(year_of_harvest__isnull=True)


from django import forms


class ChangeCreationDateForm(forms.Form):
    year = forms.IntegerField(
        label="Год", min_value=1900, max_value=2100,
        initial=lambda: timezone.now().year,
        widget=forms.NumberInput(attrs={"style": "width:6em"}),
    )
    month = forms.IntegerField(
        label="Месяц", min_value=1, max_value=12, initial=1,
        widget=forms.NumberInput(attrs={"style": "width:4em"}),
    )
    day = forms.IntegerField(
        label="День", min_value=1, max_value=31, initial=1,
        widget=forms.NumberInput(attrs={"style": "width:4em"}),
    )
    hour = forms.IntegerField(
        label="Часы", min_value=0, max_value=23, initial=0,
        widget=forms.NumberInput(attrs={"style": "width:4em"}),
    )
    minute = forms.IntegerField(
        label="Минуты", min_value=0, max_value=59, initial=0,
        widget=forms.NumberInput(attrs={"style": "width:4em"}),
    )
    _selected_action = forms.CharField(widget=forms.MultipleHiddenInput)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        try:
            naive = datetime(
                cleaned["year"], cleaned["month"], cleaned["day"],
                cleaned["hour"], cleaned["minute"],
            )
        except ValueError as exc:
            raise forms.ValidationError(f"Некорректная дата: {exc}.") from exc
        cleaned["new_created_at"] = timezone.make_aware(naive)
        return cleaned



@admin.register(Tea)
class TeaAdmin(admin.ModelAdmin):

    list_display = ('id', 'availability', 'tea_type', 'name', 
                    'place_of_purchase', 'price', 'mark', 'photo', 'place_of_harvest', 'year_of_harvest',
                    'created_at','updated_at')
    
    inlines = [TeaImageInline]

    list_display_links = ('id', 'availability','name','tea_type', 'place_of_harvest')
    
    search_fields = ('id','availability', 'tea_type','name', 'price','description')

    list_filter = ('availability', 'tea_type',
                      'place_of_harvest',InputFilter,'year_of_harvest','place_of_purchase','name', )
    
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
    save_on_top = True

    actions = ['make_available', 'make_unavailable', 'mark_as_new', 'mark_as_old','set_year_created_at','change_creation_date',
                'copy_selected', 'copy_selected_with_images', 'copy_and_mark_as_new', 'delete_selected', 'delete_selected_with_images']
    @admin.action(description='Make available')
    def make_available(self, request, queryset):
        queryset.update(availability=True)

    @admin.action(description='Make unavailable')
    def make_unavailable(self, request, queryset):
        queryset.update(availability=False)

    

    @admin.action(description='Mark as new')
    def mark_as_new(self, request, queryset):
        queryset.update(created_at=timezone.now())

    @admin.action(description='Mark as old')
    def mark_as_old(self, request, queryset):
        queryset.update(created_at=timezone.now() - timedelta(days=91))


    @admin.action(description='Set creation date like year of harvest')
    def set_year_created_at(self, request, queryset):
        for tea in queryset:
            tea.created_at = tea.created_at.replace(year=tea.year_of_harvest)
            tea.save()

    @admin.action(description='Change creation date on any')
    def change_creation_date(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        if 'apply' in request.POST:
            form = ChangeCreationDateForm(request.POST)
            if form.is_valid():
                new_created_at = form.cleaned_data['new_created_at']
                count = queryset.update(created_at=new_created_at)
                self.message_user(
                    request,
                    f'Дата создания изменена у {count} записей на {new_created_at:%Y-%m-%d %H:%M}.',
                    messages.SUCCESS,
                )
                return HttpResponseRedirect(request.get_full_path())
        else:
            form = ChangeCreationDateForm(
                initial={'_selected_action': queryset.values_list('pk', flat=True)}
            )
        return TemplateResponse(
            request,
            'admin/change_creation_date.html',
            {
                'title': 'Изменение даты создания',
                'opts': self.model._meta,
                'queryset': queryset,
                'form': form,
                'action_name': 'change_creation_date',
            },
        )

    @admin.action(description='Copy selected')
    def copy_selected(self, request, queryset):
        for tea in queryset:
            tea.pk = None
            tea.save()

    @admin.action(description='Copy selected with images')
    def copy_selected_with_images(self, request, queryset):
        for tea in queryset:
            images = tea.images.all()
            tea.pk = None
            tea.save()
            for image in images:
                image.pk = None
                image.tea = tea
                image.save()

    @admin.action(description='Copy and mark as new')
    def copy_and_mark_as_new(self, request, queryset):
        for tea in queryset:
            tea.pk = None
            tea.save()

    @admin.action(description='Delete selected')
    def delete_selected(self, request, queryset):
        queryset.delete()

    @admin.action(description='Delete selected with images')
    def delete_selected_with_images(self, request, queryset):
        for tea in queryset:
            images = tea.images.all()
            for image in images:
                image.delete()
            tea.delete()

    
