"""
URL configuration for tnr_system project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView

from business.views_portal import (
    adopter_portal, adoption_hall_public, hospital_portal, shelter_portal,
    gov_portal,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('accounts.urls')),
    path('portal/', TemplateView.as_view(template_name='portal/index.html'), name='portal'),
    path('shelter/', shelter_portal, name='shelter_home'),
    path('hospital/', hospital_portal, name='hospital_home'),
    path('adopter/', adopter_portal, name='adopter_home'),
    path('adopter/hall/', adoption_hall_public, name='adoption_hall_public'),
    path('gov/', gov_portal, name='gov_home'),
    path('api/business/', include('business.urls')),
    path('api/supervision/', include('supervision.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
