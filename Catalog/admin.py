

from django.contrib import admin

from Catalog.models import Tea, TeaImage

# Register your models here.

# admin.site.register(Tea)
from django.utils import timezone
from datetime import timedelta
class TeaImageInline(admin.TabularInline):
    model = TeaImage
    extra = 1
    readonly_fields = ('created_at', 'updated_at')
@admin.register(Tea)
class TeaAdmin(admin.ModelAdmin):
    list_display = ('id', 'availability', 'tea_type', 'name', 
                    'place_of_purchase', 'price', 'mark', 'photo', 'place_of_harvest', 'year_of_harvest',
                    'created_at','updated_at')
    inlines = [TeaImageInline]
    list_display_links = ('id', 'availability','name','tea_type', 'place_of_harvest')
    
    search_fields = ('id','availability', 'tea_type','name', 'price','description')

    list_filter = ('availability', 'tea_type',
                      'place_of_harvest','year_of_harvest','place_of_purchase','name')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
    save_on_top = True

    actions = ['make_available', 'make_unavailable', 'mark_as_new', 'mark_as_old', 'copy_selected', 'copy_selected_with_images', 'copy_and_mark_as_new', 'delete_selected', 'delete_selected_with_images']
    @admin.action(description='Make available')
    def make_available(self, request, queryset):
        queryset.update(availability=True)

    @admin.action(description='Make unavailable')
    def make_unavailable(self, request, queryset):
        queryset.update(availability=False)

    @admin.action(description='Delete selected')
    def delete_selected(self, request, queryset):
        queryset.delete()

    @admin.action(description='Mark as new')
    def mark_as_new(self, request, queryset):
        queryset.update(created_at=timezone.now())

    @admin.action(description='Mark as old')
    def mark_as_old(self, request, queryset):
        queryset.update(created_at=timezone.now() - timedelta(days=91))

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

    @admin.action(description='Delete selected with images')
    def delete_selected_with_images(self, request, queryset):
        for tea in queryset:
            images = tea.images.all()
            for image in images:
                image.delete()
            tea.delete()

    
