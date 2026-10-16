from django.contrib import admin

from Catalog.models import Tea, TeaImage

# Register your models here.

# admin.site.register(Tea)

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

    actions = ['make_available', 'make_unavailable', 'delete_selected']
    @admin.action(description='Make available')
    def make_available(self, request, queryset):
        queryset.update(availability=True)

    @admin.action(description='Make unavailable')
    def make_unavailable(self, request, queryset):
        queryset.update(availability=False)

